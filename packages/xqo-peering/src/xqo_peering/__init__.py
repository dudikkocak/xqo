"""Peering plugin: separate HQs exchange messages and files (exchange folder or HTTP peer API).

On when "peering" is in agents.json. Adds the list_peers / send_to_peer tools to allowed agents, the /api/peers
routes, the peer listener (/peer/v1/*) and `xqo peers`."""
from __future__ import annotations

from pathlib import Path

import json
import sys

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from xqo.api import Draining, MessageBus
from xqo.api import client_api as _api
from xqo.api import Plugin
from .hub import PeerError, PeerHub

PEER_TOOLS = [
    {
        "name": "list_peers",
        "description": "List the other HQs (separate organisations with their own agents) you may "
                       "send messages or files to, and what each accepts.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "send_to_peer",
        "description": "Send a message and/or files from the task workspace to another HQ. A message "
                       "starts a task there; with want_reply=true its final answer comes back into this task. Use "
                       "reply=true to send files or an answer back to the xqo whose message started "
                       "this task.",
        "input_schema": {"type": "object",
                         "properties": {"peer": {"type": "string"},
                                        "message": {"type": "string"},
                                        "files": {"type": "array", "items": {"type": "string"},
                                                  "description": "Paths in the shared task workspace (text or "
                                                                 "binary files)."},
                                        "attachments": {"type": "array", "items": {"type": "string"},
                                                        "description": "Names of the task's attachments (images, "
                                                                       "PDFs, text files) to pass on."},
                                        "want_reply": {"type": "boolean"},
                                        "reply": {"type": "boolean"}},
                         "required": ["peer"], "additionalProperties": False},
    },
]


class PeerSendIn(BaseModel):
    peer: str = Field(min_length=1, max_length=64)
    text: str = Field(default="", max_length=100_000)
    files: list[str] = Field(default_factory=list, max_length=20)  # paths in task_id's workspace
    attachments: list[str] = Field(default_factory=list, max_length=20)  # names of task_id's attachments
    task_id: str | None = Field(default=None, max_length=100)
    want_reply: bool = False


def _peers(a) -> None:
    """peers [status] / send / token: peering of separate HQs (through the running server)."""
    if a.peer_cmd == "token":
        import secrets
        print(secrets.token_urlsafe(32))
        return
    try:
        if a.peer_cmd == "send":
            r = _api(a.url, "POST", "/api/peers/send", {"peer": a.peer, "text": a.text, "files": a.file,
                                                         "attachments": a.attachment, "task_id": a.task,
                                                         "want_reply": a.want_reply})
            extra = (f": {r['detail'].get('error')} (retried in the background)" if r["status"] == "queued"
                     else f": task {r['detail']['task_id']} there" if (r.get("detail") or {}).get("task_id") else "")
            print(f"{r['status']} to {r['peer']} ({r['id']}){extra}")
            return
        st = _api(a.url, "GET", "/api/peers")
        if not st.get("enabled"):
            print("peering is not configured (\"peering\" in agents.json)")
            return
        print(f"this xqo: {st['name']}  exchange: {st['exchange_dir'] or '-'}  listen: {st['listen'] or '-'}"
              f"  queued: {st['queued']}")
        for p in st["peers"]:
            print(f"  {p['name']:<16} {p['transport']:<8} send={','.join(p['send']) or '-':<15} "
                  f"receive={','.join(p['receive']) or '-':<15} {p['url'] or ''}"
                  + ("" if p["transport"] == "exchange" or p["token_set"] else f"  (token {p['token_env']} not set)"))
        for m in _api(a.url, "GET", f"/api/peers/messages?limit={a.limit}"):
            arrow = "->" if m["dir"] == "out" else "<-"
            print(f"{m['ts'][:19]}  {arrow} {m.get('peer') or '?':<14} {m.get('kind') or '':<8} {m.get('status'):<9} "
                  f"{(m.get('text') or m.get('detail') or '')[:70]!r}")
    except RuntimeError as e:
        sys.exit(str(e))
    except ConnectionError:
        sys.exit(f"no server answering at {a.url}")


def create_peer_app(bus: MessageBus) -> FastAPI:
    """The peer API for other machines: only the inbox, behind per-peer bearer tokens. Served on its own
    listener ("peering.listen") so the dashboard can stay on localhost."""
    hub = bus.plugins.service("peering")
    app = FastAPI(title="HQ peer API", docs_url=None, redoc_url=None, openapi_url=None)

    def auth(peer: str | None, authorization: str | None):
        token = (authorization or "").removeprefix("Bearer ").strip()
        try:
            return hub.authenticate(peer or "", token)
        except PeerError as e:
            raise HTTPException(e.status, str(e))

    @app.get("/peer/v1/ping")
    async def ping(x_xqo_peer: str | None = Header(default=None), authorization: str | None = Header(default=None)):
        p = auth(x_xqo_peer, authorization)
        return {"name": hub.name, "you": p.name, "accepts": sorted(p.receive)}

    @app.post("/peer/v1/inbox")
    async def inbox(request: Request, x_xqo_peer: str | None = Header(default=None),
                    authorization: str | None = Header(default=None)):
        p = auth(x_xqo_peer, authorization)
        if int(request.headers.get("content-length") or 0) > hub.limits.max_body_bytes:
            raise HTTPException(413, f"envelope too large for {hub.name} (peering.limits)")
        raw = await request.body()
        if len(raw) > hub.limits.max_body_bytes:
            raise HTTPException(413, f"envelope too large for {hub.name} (peering.limits)")
        try:
            env = json.loads(raw)
        except ValueError:
            raise HTTPException(422, "body must be JSON")
        if not isinstance(env, dict) or env.get("from") != p.name:
            raise HTTPException(403, "the envelope's sender does not match X-XQO-Peer")
        try:
            return await hub.receive(env, "http")
        except PeerError as e:
            raise HTTPException(e.status, str(e))
        except Draining as e:  # before an upgrade: the sender queues it and retries
            raise HTTPException(503, str(e))

    return app


class PeeringPlugin(Plugin):
    name = "peering"
    description = "Messages and files between separate HQs"
    service = "peers"  # also reachable as bus.peers; the office uses bus.plugins.service("peering")
    config_keys = ("peering",)
    state_dirs = (".peers",)
    tool_names = frozenset({"list_peers", "send_to_peer"})
    nav = {"id": "peersb", "label": "peers"}
    static = ("/plugins/peering", Path(__file__).parent / "static")
    ui = ("ui.js",)

    @classmethod
    def add_cli(cls, sub, url) -> list[str]:
        pe = sub.add_parser("peers", help="peering with other HQs: status, send, token")
        pe.add_argument("--url", default=url, help="server address (XQO_URL)")
        pe.add_argument("--limit", type=int, default=20, help="recent peer messages to show")
        psub = pe.add_subparsers(dest="peer_cmd")
        ps = psub.add_parser("send", help="send a message (and workspace files) to a peer")
        ps.add_argument("peer")
        ps.add_argument("text", nargs="?", default="")
        ps.add_argument("--file", action="append", default=[], metavar="PATH", help="workspace file of --task (repeatable)")
        ps.add_argument("--attachment", action="append", default=[], metavar="NAME",
                        help="attachment of --task to pass on (repeatable)")
        ps.add_argument("--task", help="task whose workspace the files come from; replies land in it")
        ps.add_argument("--want-reply", action="store_true", help="the peer sends its result back (needs --task)")
        psub.add_parser("token", help="print a new random token for a pair of peers")
        return ["peers"]

    @classmethod
    def run_cli(cls, a) -> None:
        _peers(a)

    def configure(self, ctx, cfg):
        return PeerHub.from_config(ctx.bus, cfg.get("peering"), ctx.data_root)

    async def start(self) -> None:
        self.obj.start()

    async def stop(self) -> None:
        await self.obj.stop()

    def status(self) -> dict:
        return self.obj.status()

    def banner(self) -> list[str]:
        out = [f"Peering as {self.obj.name!r}: {', '.join(self.obj.peers) or 'no peers listed'}"]
        if self.obj.listen:
            host, port = self.obj.listen
            out.append(f"Peer API: http://{host}:{port}/peer/v1/inbox")
        return out

    def side_servers(self):
        if not self.obj.listen:
            return []
        host, port = self.obj.listen
        return [(create_peer_app(self.ctx.bus), host, port)]

    def tool_defs(self, agent) -> list[dict]:
        return list(PEER_TOOLS) if self.obj.allowed(agent) else []

    def prompt_note(self, agent) -> str:
        return self.obj.describe_for(agent) if self.obj.allowed(agent) else ""

    async def call_tool(self, task, agent, name: str, inp: dict) -> str:
        peers = self.obj
        if not peers.allowed(agent):
            raise PermissionError(f"{agent.name} may not contact other xqos")
        if name == "list_peers":
            out = json.dumps([{k: p[k] for k in ("name", "description", "send", "transport")}
                              for p in peers.status()["peers"] if p["send"]])
        else:
            r = await peers.send(inp["peer"], inp.get("message", ""), inp.get("files"), task=task,
                                 agent=agent.name, want_reply=bool(inp.get("want_reply")),
                                 as_reply=bool(inp.get("reply")), attachments=inp.get("attachments"))
            out = (f"{r['status']} to {r['peer']} (id {r['id']})"
                   + (f", files: {', '.join(r['files'])}" if r["files"] else "")
                   + (". The peer is unreachable now; it is retried in the background." if r["status"] == "queued"
                      else ". Its reply will arrive in this task as a message." if inp.get("want_reply") else ""))
        return out

    async def on_task_done(self, task) -> None:
        await self.obj.task_done(task)

    def routes(self, app, bus) -> None:
        @app.get("/api/peers")
        async def peers():
            hub = bus.plugins.service("peering")
            return {"enabled": bool(hub), **(hub.status() if hub else {})}

        @app.get("/api/peers/messages")
        async def peer_messages(limit: int = 200):
            hub = bus.plugins.service("peering")
            return hub.messages(max(1, min(limit, 5000))) if hub else []

        @app.post("/api/peers/send")
        async def peer_send(body: PeerSendIn):
            """The user sends a message (and files from a task's workspace) to a peer xqo."""
            if not bus.plugins.service("peering"):
                raise HTTPException(400, "peering is not configured")
            if body.task_id and body.task_id not in bus.tasks:
                raise HTTPException(404, f"unknown task {body.task_id}")
            try:
                return await bus.plugins.service("peering").send(body.peer, body.text, body.files, root_id=body.task_id, agent="user",
                                            want_reply=body.want_reply, attachments=body.attachments)
            except PeerError as e:
                raise HTTPException(e.status, str(e))
            except (KeyError, ValueError, OSError) as e:
                raise HTTPException(400, str(e))
