"""Peering: opt-in messages and workspace files between separate HQ instances.

Each HQ runs on its own (process or container, data dir, port, departments). Peering connects them, and only
for the peers listed in agents.json:

    "peering": {
      "name": "eng",                      # this instance's id (XQO_PEER_NAME); peers address it by this name
      "exchange_dir": "/exchange",        # shared folder for peers on the same machine / volume (XQO_EXCHANGE_DIR)
      "listen": "0.0.0.0:8100",           # optional peer-only HTTP listener for other machines (XQO_PEER_LISTEN)
      "agents": "heads",                  # who gets the peer tools: "heads" (HQ + department heads), "*" or names
      "entry_agent": null,                # agent that takes an incoming message (default: the first agent)
      "max_hops": 3,                      # peer -> peer -> peer chains longer than this are refused
      "peers": {
        "marketing": {"send": true, "receive": ["messages"]},                       # via exchange_dir
        "lab": {"url": "http://10.0.0.7:8100", "token_env": "PEER_LAB_TOKEN"}}}     # via HTTP

"send" / "receive": true (default: messages and files), false, or a list of "messages" / "files"; a reply
counts as a message. Unlisted peers are refused both ways. Sending: a peer with "url" is reached over HTTP with a
bearer token (one shared secret per pair of instances, read from the environment), one without "url" through the
exchange folder. Receiving: from the exchange folder (when set) and, with "listen" and the peer's token set,
over HTTP. When a peer has a token, exchange envelopes are signed (HMAC-SHA256) and the signature is required.

An incoming message starts a task (its files land in the workspace under peers/<from>/); with want_reply its
final answer goes back automatically. A reply lands in the task that sent the message: queued for its agents,
or the task is reopened when it has finished.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import mimetypes
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from xqo.api import max_file_bytes
from xqo.api import safe_name, validate_attachments
from xqo.api import HQ
from xqo.api import normalize_path
from xqo.api import Event, new_id, utcnow

KINDS = ("messages", "files")
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
# defaults of "peering.limits" (each one also settable with an XQO_PEER_* environment variable, which wins)
LIMIT_DEFAULTS = {
    "max_files": 20,          # files + attachments per envelope          XQO_PEER_MAX_FILES
    "max_file_mb": 20,        # one file                                   XQO_PEER_MAX_FILE_MB
    "max_total_mb": 20,       # all files of one envelope                  XQO_PEER_MAX_TOTAL_MB
    "max_text_chars": 100_000,  # message text                             XQO_PEER_MAX_TEXT_CHARS
    "retry_seconds": 30,      # between delivery attempts of queued HTTP sends   XQO_PEER_RETRY_SECONDS
    "queue_hours": 24,        # queued HTTP sends older than this are dropped    XQO_PEER_QUEUE_HOURS
}


class Limits:
    def __init__(self, d: dict | None = None):
        d = dict(d or {})
        bad = set(d) - set(LIMIT_DEFAULTS)
        if bad:
            raise ValueError(f"peering.limits: unknown setting(s) {', '.join(sorted(bad))}; "
                             f"known: {', '.join(LIMIT_DEFAULTS)}")
        v = {}
        for k, default in LIMIT_DEFAULTS.items():
            raw = os.getenv(f"XQO_PEER_{k.upper()}", d.get(k, default))
            try:
                v[k] = float(raw)
            except (TypeError, ValueError):
                raise ValueError(f"peering.limits.{k} must be a number, got {raw!r}") from None
            if v[k] <= 0:
                raise ValueError(f"peering.limits.{k} must be positive")
        self.values = {k: (int(x) if x == int(x) else x) for k, x in v.items()}
        self.max_files = int(v["max_files"])
        self.max_file_bytes = int(v["max_file_mb"] * 1_000_000)
        self.max_total_bytes = int(v["max_total_mb"] * 1_000_000)
        self.max_text = int(v["max_text_chars"])
        self.retry_seconds = v["retry_seconds"]
        self.queue_seconds = v["queue_hours"] * 3600

    @property
    def max_body_bytes(self) -> int:
        """Largest HTTP envelope the peer listener accepts: base64 grows binary data by a third."""
        return self.max_total_bytes * 4 // 3 + self.max_text * 4 + 1_000_000


class PeerError(RuntimeError):
    """A send or receive was refused; .status is the HTTP status the peer API answers with."""

    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


def _kinds(value, where: str) -> set[str]:
    if value is True or value is None:
        return set(KINDS)
    if value is False:
        return set()
    items = {"files" if k == "artifacts" else k for k in (value if isinstance(value, list) else [value])}
    bad = items - set(KINDS)
    if bad:
        raise ValueError(f"peering: {where} must be true, false or a list of {', '.join(KINDS)} (got {sorted(bad)})")
    return items


def _canonical(env: dict) -> bytes:
    return json.dumps({k: v for k, v in env.items() if k != "sig"}, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode()


def sign(env: dict, token: str) -> str:
    return hmac.new(token.encode(), _canonical(env), hashlib.sha256).hexdigest()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_text(data: bytes) -> bool:
    if b"\x00" in data[:8192]:
        return False
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def file_item(name: str, data: bytes, attachment: bool = False, media_type: str | None = None) -> dict:
    """One file of an envelope: UTF-8 text as "content", anything else base64 as "data" (with its media type).
    attachment: a task attachment (image, PDF, text) rather than a workspace file."""
    item = {"name": name, "bytes": len(data), "sha256": _sha(data)}
    if attachment:
        item["attachment"] = True
    if _is_text(data) and not attachment and len(data) <= max_file_bytes():  # larger text goes as bytes
        return {**item, "content": data.decode("utf-8")}
    return {**item, "data": base64.b64encode(data).decode(),
            "media_type": media_type or mimetypes.guess_type(name)[0] or "application/octet-stream"}


def file_bytes(item: dict) -> bytes:
    if isinstance(item.get("content"), str):
        return item["content"].encode("utf-8")
    return base64.b64decode(item["data"], validate=True)


def parse_listen(value: str | None) -> tuple[str, int] | None:
    """"0.0.0.0:8100" / ":8100" / "8100" -> (host, port); None or "" = no listener."""
    if not value:
        return None
    host, _, port = str(value).rpartition(":")
    try:
        return host or "0.0.0.0", int(port)
    except ValueError:
        raise ValueError(f"peering.listen must be host:port, got {value!r}") from None


class Peer:
    def __init__(self, name: str, d: dict):
        if not NAME.match(name):
            raise ValueError(f"peering: bad peer name {name!r} (letters, digits, '_', '.', '-')")
        self.name = name
        self.url = (d.get("url") or "").rstrip("/") or None
        if self.url and not self.url.startswith(("http://", "https://")):
            raise ValueError(f"peering: peer {name!r} url must start with http:// or https://")
        if d.get("token"):
            raise ValueError(f"peering: peer {name!r} has a plaintext token; put it in an environment variable "
                             "and name that with token_env")
        self.token_env = d.get("token_env") or None
        self.send = _kinds(d.get("send", True), f"peers.{name}.send")
        self.receive = _kinds(d.get("receive", True), f"peers.{name}.receive")
        self.entry_agent = d.get("entry_agent") or None
        self.description = d.get("description", "")

    @property
    def transport(self) -> str:
        return "http" if self.url else "exchange"

    def token(self) -> str:
        return os.getenv(self.token_env or "", "") if self.token_env else ""

    def public(self) -> dict:
        return {"name": self.name, "transport": self.transport, "url": self.url, "description": self.description,
                "send": sorted(self.send), "receive": sorted(self.receive), "token_env": self.token_env,
                "token_set": bool(self.token()), "entry_agent": self.entry_agent}


class PeerHub:
    def __init__(self, bus, cfg: dict, data_root: Path):
        self.bus = bus
        self.name = os.getenv("XQO_PEER_NAME") or cfg.get("name") or ""
        if not NAME.match(self.name):
            raise ValueError("peering.name (or XQO_PEER_NAME) must be set: letters, digits, '_', '.', '-'")
        ex = os.getenv("XQO_EXCHANGE_DIR", cfg.get("exchange_dir") or "")
        self.exchange = Path(ex).expanduser().resolve() if ex else None
        self.listen = parse_listen(os.getenv("XQO_PEER_LISTEN", cfg.get("listen") or ""))
        self.agents = cfg.get("agents", "heads")
        self.entry_agent = cfg.get("entry_agent") or None
        self.max_hops = int(cfg.get("max_hops", 3))
        self.limits = Limits(cfg.get("limits"))
        self.poll = float(cfg.get("poll_seconds", 2))
        self.peers = {n: Peer(n, d or {}) for n, d in (cfg.get("peers") or {}).items()}
        if self.name in self.peers:
            raise ValueError(f"peering: {self.name!r} lists itself as a peer")
        need = [p.name for p in self.peers.values() if p.transport == "exchange" and p.send]
        if need and not self.exchange:
            raise ValueError(f"peering: {', '.join(need)} may be sent to but have no url, so exchange_dir must be set")
        self.dir = data_root / ".peers"
        (self.dir / "outbox").mkdir(parents=True, exist_ok=True)
        self.log_path = self.dir / "messages.jsonl"
        self._lock = threading.Lock()
        self.seen: set[str] = set()            # envelope ids received (duplicates are acknowledged, not redone)
        self.outbound: dict[str, dict] = {}    # envelope id -> {peer, root} for replies
        self.inbound: dict[str, dict] = {}     # root task id -> the envelope that started it (no files)
        self._jobs: list[asyncio.Task] = []
        self._load()

    @classmethod
    def from_config(cls, bus, cfg: dict | None, data_root: Path) -> "PeerHub | None":
        if not cfg or cfg.get("enabled") is False or os.getenv("XQO_PEERING") in ("0", "false", "off"):
            return None
        return cls(bus, cfg, data_root)

    # ---- log ------------------------------------------------------------
    def _load(self) -> None:
        for rec in self.messages(limit=None):
            if rec["dir"] == "in" and rec.get("status") in ("accepted", "replied"):
                self.seen.add(rec["id"])
                if rec.get("task") and rec.get("kind") == "message":
                    self.inbound[rec["task"]] = {k: rec.get(k) for k in ("id", "from", "want_reply", "hops")}
            elif rec["dir"] == "out" and rec.get("root"):
                self.outbound[rec["id"]] = {"peer": rec["peer"], "root": rec["root"]}

    def _log(self, rec: dict) -> dict:
        rec = {"ts": utcnow(), **rec}
        with self._lock, self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    def messages(self, limit: int | None = 200) -> list[dict]:
        if not self.log_path.is_file():
            return []
        out = []
        for line in self.log_path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out[-limit:] if limit else out

    def status(self) -> dict:
        outbox = sorted((self.dir / "outbox").glob("*.json"))
        return {"name": self.name, "exchange_dir": str(self.exchange) if self.exchange else None,
                "limits": self.limits.values,
                "listen": f"{self.listen[0]}:{self.listen[1]}" if self.listen else None,
                "agents": self.agents, "max_hops": self.max_hops,
                "peers": [p.public() for p in self.peers.values()], "queued": len(outbox)}

    # ---- who may use it -------------------------------------------------
    def allowed(self, agent) -> bool:
        if not self.peers:
            return False
        if self.agents == "*":
            return True
        if self.agents == "heads":
            return agent.department == HQ or agent.is_head
        return agent.name in (self.agents or [])

    def describe_for(self, agent) -> str:
        rows = [f"- {p.name}: may send {' and '.join(sorted(p.send)) or 'nothing'}"
                + (f" ({p.description})" if p.description else "") for p in self.peers.values() if p.send]
        return ("Other HQs you can reach with send_to_peer (they have their own agents and workspaces):\n"
                + "\n".join(rows)) if rows else ""

    # ---- sending --------------------------------------------------------
    async def send(self, peer_name: str, text: str = "", files: list[str] | None = None, *, task=None,
                   agent: str = "user", want_reply: bool = False, as_reply: bool = False,
                   root_id: str | None = None, attachments: list[str] | None = None) -> dict:
        """Send a message, workspace files (paths in the root task's workspace, text or binary) and/or the
        task's attachments (by name: images, PDFs, text files) to a peer."""
        peer = self.peers.get(peer_name)
        if not peer:
            raise PeerError(f"unknown peer {peer_name!r}; known: {', '.join(self.peers) or 'none'}", 404)
        text, files = (text or "").strip(), list(dict.fromkeys(files or []))
        atts = list(dict.fromkeys(attachments or []))
        if not text and not files and not atts:
            raise PeerError("nothing to send: write a message or name files", 422)
        if text and "messages" not in peer.send:
            raise PeerError(f"sending messages to {peer.name} is not allowed (peering.peers.{peer.name}.send)", 403)
        if (files or atts) and "files" not in peer.send:
            raise PeerError(f"sending files to {peer.name} is not allowed (peering.peers.{peer.name}.send)", 403)
        lim = self.limits
        if len(text) > lim.max_text or len(files) + len(atts) > lim.max_files:
            raise PeerError(f"too much to send: at most {lim.max_text} characters and {lim.max_files} files "
                            "(peering.limits)", 413)
        root = root_id or (task.root_id if task else None)
        if (files or atts) and not root:
            raise PeerError("files are sent from a task's workspace: name the task", 422)
        origin = self.inbound.get(root or "")
        in_reply_to = None
        if as_reply:
            if not origin or origin["from"] != peer.name:
                raise PeerError(f"this task was not started by a message from {peer.name}: send without reply", 409)
            in_reply_to = origin["id"]
        hops = (origin["hops"] if origin else 0) + (0 if as_reply else 1)
        if hops > self.max_hops:
            raise PeerError(f"refused: this would be hop {hops} of a peer chain (max_hops {self.max_hops})", 409)
        items = []
        for f in files:
            name, data = self.bus.artifacts.read_bytes(root, f)
            items.append(file_item(name, data))
        for a in atts:
            p = self.bus.attachments.path(root, a)
            if not p:
                raise PeerError(f"unknown attachment {a!r} in this task", 404)
            meta = next((m for m in getattr(self.bus.tasks.get(root), "attachments", []) if m["name"] == p.name), {})
            items.append(file_item(p.name, p.read_bytes(), attachment=True, media_type=meta.get("media_type")))
        total = 0
        for i in items:
            total += i["bytes"]
            if i["bytes"] > lim.max_file_bytes or total > lim.max_total_bytes:
                raise PeerError(f"{i['name']}: too large to send (max {lim.max_file_bytes} bytes a file, "
                                f"{lim.max_total_bytes} in all; peering.limits)", 413)
        env = {"v": 1, "id": new_id("pmsg"), "from": self.name, "to": peer.name, "ts": utcnow(),
               "kind": "reply" if as_reply else "message", "text": text, "files": items,
               "want_reply": bool(want_reply and not as_reply), "in_reply_to": in_reply_to, "hops": hops,
               "origin": {"task": root, "agent": agent}}
        if peer.token():
            env["sig"] = sign(env, peer.token())
        status, detail = await self._deliver(peer, env)
        rec = self._log({"dir": "out", "id": env["id"], "peer": peer.name, "kind": env["kind"], "agent": agent,
                         "root": root, "text": text[:500], "files": [i["name"] for i in items if not i.get("attachment")],
                         "attachments": [i["name"] for i in items if i.get("attachment")],
                         "want_reply": env["want_reply"], "in_reply_to": in_reply_to, "status": status,
                         "transport": peer.transport, "detail": detail})
        if root:
            self.outbound[env["id"]] = {"peer": peer.name, "root": root}
        self._emit(agent, f"peer:{peer.name}", task, f"{'reply' if as_reply else 'message'} to {peer.name}"
                   f" ({status}): {text[:200]}" + (f" [+{len(items)} file(s)]" if items else ""), rec)
        return {"id": env["id"], "peer": peer.name, "status": status, "detail": detail,
                "files": [i["name"] for i in items]}

    async def _deliver(self, peer: Peer, env: dict) -> tuple[str, dict | None]:
        if peer.transport == "exchange":
            await asyncio.to_thread(self._drop, peer.name, env)
            return "delivered", None
        try:
            return "delivered", await asyncio.to_thread(self._post, peer, env)
        except PeerError:
            raise
        except (OSError, urllib.error.URLError) as e:  # unreachable: keep it and retry in the background
            self._queue(env)
            return "queued", {"error": str(e)[:300]}

    def _drop(self, to: str, env: dict) -> None:
        """Write an envelope into a peer's exchange inbox; the rename makes it appear complete."""
        inbox = self.exchange / to / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        tmp = self.exchange / to / f".{env['id']}.tmp"
        tmp.write_text(json.dumps(env, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, inbox / f"{env['id']}.json")

    def _post(self, peer: Peer, env: dict) -> dict:
        token = peer.token()
        if not token:
            raise PeerError(f"no token for {peer.name}: set {peer.token_env or 'token_env'} in the environment", 400)
        req = urllib.request.Request(peer.url + "/peer/v1/inbox", method="POST",
                                     data=json.dumps(env, ensure_ascii=False).encode(),
                                     headers={"Content-Type": "application/json", "X-XQO-Peer": self.name,
                                              "Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read()).get("detail")
            except ValueError:
                detail = e.reason
            if e.code >= 500 or e.code == 429:
                raise OSError(f"{e.code}: {detail}") from None
            raise PeerError(f"{peer.name} refused it ({e.code}): {detail}", e.code) from None

    def _queue(self, env: dict) -> None:
        p = self.dir / "outbox" / f"{env['id']}.json"
        p.write_text(json.dumps({"queued": time.time(), "env": env}, ensure_ascii=False), encoding="utf-8")

    async def retry_outbox(self) -> int:
        """One delivery attempt for every queued HTTP envelope; returns how many went out."""
        sent = 0
        for p in sorted((self.dir / "outbox").glob("*.json")):
            try:
                item = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                p.unlink(missing_ok=True)
                continue
            env, peer = item["env"], self.peers.get(item["env"]["to"])
            status, detail = None, None
            if not peer or not peer.url:
                status, detail = "dropped", "peer no longer configured for HTTP"
            elif time.time() - item["queued"] > self.limits.queue_seconds:
                status, detail = "dropped", f"not delivered within {self.limits.values['queue_hours']} h"
            else:
                try:
                    await asyncio.to_thread(self._post, peer, env)
                    status, sent = "delivered", sent + 1
                except PeerError as e:
                    status, detail = "refused", str(e)
                except (OSError, urllib.error.URLError):
                    continue  # still unreachable: try again later
            p.unlink(missing_ok=True)
            rec = self._log({"dir": "out", "id": env["id"], "peer": env["to"], "kind": env["kind"], "status": status,
                             "detail": detail, "retry": True, "root": (env.get("origin") or {}).get("task")})
            self._emit("peering", f"peer:{env['to']}", None, f"queued {env['kind']} to {env['to']}: {status}"
                       + (f" ({detail})" if detail else ""), rec, root=rec.get("root"))
        return sent

    # ---- receiving ------------------------------------------------------
    def authenticate(self, sender: str, token: str) -> Peer:
        """HTTP peer API: the sender must be a listed peer with a token set here, and send that token."""
        peer = self.peers.get(sender or "")
        if not peer:
            raise PeerError("unknown peer", 403)
        expected = peer.token()
        if not expected:
            raise PeerError(f"no token configured for {sender} on {self.name}", 403)
        if not hmac.compare_digest(expected.encode(), (token or "").encode()):
            raise PeerError("bad token", 401)
        return peer

    def _check(self, env: dict, transport: str) -> Peer:
        if not isinstance(env, dict) or env.get("v") != 1 or not isinstance(env.get("id"), str) \
                or not NAME.match(env["id"]):
            raise PeerError("not a peer envelope (v1)", 422)
        peer = self.peers.get(env.get("from") or "")
        if not peer:
            raise PeerError(f"{env.get('from')!r} is not a peer of {self.name}", 403)
        if env.get("to") != self.name:
            raise PeerError(f"addressed to {env.get('to')!r}, this is {self.name!r}", 422)
        if transport == "exchange" and peer.token():
            if not hmac.compare_digest(str(env.get("sig") or ""), sign(env, peer.token())):
                raise PeerError(f"bad or missing signature from {peer.name}", 401)
        text, files = env.get("text") or "", env.get("files") or []
        if not isinstance(text, str) or not isinstance(files, list) or len(text) > self.limits.max_text \
                or len(files) > self.limits.max_files:
            raise PeerError("message too large or malformed", 413)
        if env.get("kind") not in ("message", "reply"):
            raise PeerError("kind must be message or reply", 422)
        if (text or env["kind"] == "reply") and "messages" not in peer.receive:
            raise PeerError(f"{self.name} does not accept messages from {peer.name}", 403)
        if files and "files" not in peer.receive:
            raise PeerError(f"{self.name} does not accept files from {peer.name}", 403)
        total = 0
        for f in files:
            if not isinstance(f, dict) or not isinstance(f.get("name"), str) \
                    or isinstance(f.get("content"), str) == isinstance(f.get("data"), str):
                raise PeerError("malformed file entry: needs a name and either content or data", 422)
            if not (safe_name(f["name"]) if f.get("attachment") else normalize_path(f["name"])):
                raise PeerError(f"bad file name {f['name']!r}", 422)
            if len(f.get("data") or "") > self.limits.max_file_bytes * 4 // 3 + 4:
                raise PeerError(f"file too large for {self.name} (peering.limits: {self.limits.values['max_file_mb']} "
                                "MB a file)", 413)
            try:
                data = file_bytes(f)
            except (binascii.Error, ValueError):
                raise PeerError(f"{f['name']}: data is not valid base64", 422) from None
            total += len(data)
            if len(data) > self.limits.max_file_bytes or total > self.limits.max_total_bytes:
                raise PeerError(f"file too large for {self.name} (peering.limits: {self.limits.values['max_file_mb']} MB "
                                f"a file, {self.limits.values['max_total_mb']} MB in all)", 413)
            if "content" in f and len(data) > max_file_bytes():
                raise PeerError(f"{f['name']}: text files are limited to {max_file_bytes()} bytes "
                                "(limits.workspace_file_mb)", 413)
            if f.get("sha256") != _sha(data):
                raise PeerError(f"{f['name']}: checksum mismatch", 422)
        if not isinstance(env.get("hops", 0), int) or env.get("hops", 0) > self.max_hops:
            raise PeerError(f"peer chain too long (max_hops {self.max_hops})", 409)
        return peer

    async def receive(self, env: dict, transport: str) -> dict:
        """Accept one envelope: a message starts a task, a reply goes to the task that asked."""
        peer = self._check(env, transport)
        if env["id"] in self.seen:
            return {"accepted": True, "duplicate": True, "id": env["id"]}
        self.seen.add(env["id"])
        try:
            if env["kind"] == "reply":
                return await self._reply_in(peer, env, transport)
            return await self._message_in(peer, env, transport)
        except Exception:
            self.seen.discard(env["id"])
            raise

    def _land(self, root: str, peer: Peer, env: dict, task_id: str) -> list[str]:
        """Write the envelope's workspace files under peers/<sender>/: text as text, anything else byte for byte."""
        out = []
        for f in env.get("files") or []:
            if f.get("attachment"):
                continue
            rel = f"peers/{peer.name}/{normalize_path(f['name'])}"
            src = f"peer:{peer.name}:{env['id']}"
            if "content" in f:
                rec = self.bus.artifacts.write_file(root, rel, f["content"], task_id, f"peer:{peer.name}",
                                                    action="peer", source=src)
            else:
                rec = self.bus.artifacts.write_bytes(root, rel, file_bytes(f), task_id, f"peer:{peer.name}",
                                                     action="peer", source=src)
            self.bus.emit(Event("artifact", f"peer:{peer.name}", None, task_id, None, f"peer: {rec['file']}", rec))
            out.append(rec["file"])
        return out

    def _attachments(self, env: dict) -> list[dict]:
        """Validated attachment items for the receiving task: the envelope's attachments, plus a viewable copy of
        every image or PDF among its workspace files (so agents can look at them with read_attachment)."""
        explicit, extra = [], []
        for f in env.get("files") or []:
            data = file_bytes(f)
            if f.get("attachment"):
                explicit.append((safe_name(f["name"]), f.get("media_type"), data))
            elif "data" in f:
                extra.append((Path(normalize_path(f["name"])).name, f.get("media_type"), data))
        try:
            out = validate_attachments(explicit)
        except ValueError as e:
            raise PeerError(f"attachment refused: {e}", 422) from None
        for item in extra:
            try:
                out += validate_attachments([item])
            except ValueError:
                continue  # not an image or PDF (or too large to show): it stays a workspace file only
        try:
            return validate_attachments([(o["name"], o["media_type"], o["data"]) for o in out]) if out else []
        except ValueError as e:
            raise PeerError(f"attachments refused: {e}", 413) from None

    async def _message_in(self, peer: Peer, env: dict, transport: str) -> dict:
        agent = peer.entry_agent or self.entry_agent
        if agent and agent not in self.bus.agents:
            raise PeerError(f"entry agent {agent!r} is not configured on {self.name}", 503)
        names = [normalize_path(f["name"]) for f in env.get("files") or [] if not f.get("attachment")]
        atts = self._attachments(env)
        prompt = (f"Message from the HQ '{peer.name}' (a separate organisation with its own agents; "
                  f"peer message {env['id']}):\n\n{env.get('text') or '(no text: see the files)'}")
        if names:
            prompt += "\n\nFiles it sent are in the workspace under " + f"peers/{peer.name}/: " + ", ".join(names)
            binary = [normalize_path(f["name"]) for f in env.get("files") or [] if "data" in f and not f.get("attachment")]
            if binary:
                prompt += (f" (binary, not readable as text: {', '.join(binary)}; images and PDFs among them are "
                           "also attached)")
        if env.get("want_reply"):
            prompt += (f"\n\n{peer.name} is waiting for the result: your final answer is sent back to it "
                       "automatically. To send files back, use send_to_peer with reply=true.")
        task = await self.bus.create(prompt, agent, files=atts or None)
        landed = self._land(task.root_id, peer, env, task.id)
        meta = {"id": env["id"], "from": peer.name, "want_reply": bool(env.get("want_reply")),
                "hops": int(env.get("hops", 0))}
        self.inbound[task.root_id] = meta
        rec = self._log({"dir": "in", "id": env["id"], "peer": peer.name, "from": peer.name, "kind": "message",
                         "text": (env.get("text") or "")[:500], "files": landed,
                         "attachments": [a["name"] for a in task.attachments], "want_reply": meta["want_reply"],
                         "hops": meta["hops"], "status": "accepted", "task": task.id, "transport": transport})
        self.bus.spawn(self.bus._run(task))
        self._emit(f"peer:{peer.name}", task.assignee, task, f"message from {peer.name}: {(env.get('text') or '')[:200]}"
                   + (f" [+{len(landed)} file(s)]" if landed else "")
                   + (f" [+{len(task.attachments)} attachment(s)]" if task.attachments else ""), rec)
        return {"accepted": True, "id": env["id"], "task_id": task.id}

    async def _reply_in(self, peer: Peer, env: dict, transport: str) -> dict:
        link = self.outbound.get(env.get("in_reply_to") or "")
        root = self.bus.tasks.get(link["root"]) if link and link["peer"] == peer.name else None
        text = env.get("text") or ""
        rec = {"dir": "in", "id": env["id"], "peer": peer.name, "from": peer.name, "kind": "reply",
               "in_reply_to": env.get("in_reply_to"), "text": text[:500], "transport": transport}
        if not root or (self.bus.lifecycle and self.bus.lifecycle.entry(root.id)):
            rec = self._log({**rec, "status": "accepted", "detail": "the task that asked is gone; kept in the log"})
            self._emit(f"peer:{peer.name}", None, None, f"reply from {peer.name} (no task to deliver it to): {text[:200]}", rec)
            return {"accepted": True, "id": env["id"], "delivered": False}
        atts = self._attachments(env)
        landed = self._land(root.id, peer, env, root.id)
        metas = self.bus.attachments.save(root.id, atts) if atts else []
        if metas:
            root.attachments = list(root.attachments) + metas
        body = (f"Reply from the HQ '{peer.name}' to your peer message {env.get('in_reply_to')}:\n\n"
                f"{text or '(no text)'}"
                + (f"\n\nFiles it sent are now in the workspace: {', '.join(landed)}" if landed else "")
                + (f"\n\nAttached: {', '.join(m['name'] for m in metas)} (open with read_attachment)" if metas else ""))
        msg = {"id": new_id("msg"), "kind": "note", "text": body, "to": None, "urgent": False, "by": f"peer:{peer.name}",
               **({"attachments": metas} if metas else {})}
        chat = self.bus.chat
        if self.bus.is_running(root.id):
            for t in chat._live(root.id) or [root]:
                chat._queue(t.id, dict(msg))
            how = "queued for the running task"
        else:
            chat.reopen(root.id, body, "steer", f"peer:{peer.name}", msg)
            how = "reopened the task"
        rec = self._log({**rec, "status": "accepted", "task": root.id, "files": landed,
                         "attachments": [m["name"] for m in metas], "detail": how})
        self._emit(f"peer:{peer.name}", root.assignee, root, f"reply from {peer.name} ({how}): {text[:200]}", rec)
        return {"accepted": True, "id": env["id"], "delivered": True, "task_id": root.id}

    async def task_done(self, task) -> None:
        """A task started by a peer message finished: send its final answer back when the peer asked for it."""
        origin = self.inbound.get(task.id)
        if not origin or not origin.get("want_reply") or task.id != task.root_id:
            return
        text = f"[{task.status}] {task.result or ''}".strip()[:self.limits.max_text]
        try:
            await self.send(origin["from"], text, task=task, agent=task.assignee, as_reply=True)
        except PeerError as e:
            self._emit("peering", f"peer:{origin['from']}", task, f"could not send the result back: {e}", {})

    # ---- exchange folder ------------------------------------------------
    async def poll_exchange(self) -> int:
        """Process everything waiting in this instance's exchange inbox; returns how many were handled."""
        if not self.exchange:
            return 0
        if getattr(self.bus, "on_hold", lambda: False)():  # draining or a backup: leave messages for later
            return 0
        inbox = self.exchange / self.name / "inbox"
        if not inbox.is_dir():
            return 0
        n = 0
        for p in sorted(inbox.glob("*.json")):
            claimed = p.with_suffix(".claimed")
            try:
                os.replace(p, claimed)  # another process with the same name can't take it twice
                env = json.loads(claimed.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as e:
                self._reject(claimed, {"id": p.stem}, f"unreadable: {e}")
                continue
            try:
                await self.receive(env, "exchange")
                dest = self.exchange / self.name / "processed"
            except PeerError as e:
                self._reject(claimed, env, str(e))
                continue
            except Exception as e:  # e.g. a workspace write failed: keep the envelope for a look, don't loop on it
                self._reject(claimed, env if isinstance(env, dict) else {}, f"{type(e).__name__}: {e}")
                continue
            dest.mkdir(parents=True, exist_ok=True)
            os.replace(claimed, dest / p.name)
            n += 1
        return n

    def _reject(self, path: Path, env: dict, why: str) -> None:
        dest = self.exchange / self.name / "rejected"
        dest.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(path, dest / path.with_suffix(".json").name)
        except OSError:
            pass
        rec = self._log({"dir": "in", "id": env.get("id"), "peer": env.get("from"), "from": env.get("from"),
                         "kind": env.get("kind"), "status": "rejected", "detail": why, "transport": "exchange"})
        self._emit(f"peer:{env.get('from')}", None, None, f"rejected peer envelope from {env.get('from')}: {why}", rec)

    # ---- background -----------------------------------------------------
    async def _loop(self, fn, every: float) -> None:
        while True:
            try:
                await fn()
            except Exception as e:  # never stop polling over one bad item
                self._emit("peering", None, None, f"peering error: {type(e).__name__}: {e}", {})
            await asyncio.sleep(every)

    def start(self) -> None:
        if self.exchange:
            (self.exchange / self.name / "inbox").mkdir(parents=True, exist_ok=True)
            self._jobs.append(asyncio.create_task(self._loop(self.poll_exchange, self.poll)))
        if any(p.url for p in self.peers.values()):
            self._jobs.append(asyncio.create_task(self._loop(self.retry_outbox, self.limits.retry_seconds)))

    async def stop(self) -> None:
        for j in self._jobs:
            j.cancel()
        self._jobs.clear()

    def _emit(self, sender, recipient, task, text: str, meta: dict, root: str | None = None) -> None:
        tid = task.id if task else root
        self.bus.emit(Event("peer", sender or "peering", recipient, tid, task.parent_id if task else None, text,
                            {k: v for k, v in meta.items() if k != "text"}))
