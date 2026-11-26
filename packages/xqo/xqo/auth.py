"""Optional dashboard login: Google sign-in (OpenID Connect), Telegram (a bot link) and an API token.

Off unless an "auth" block in agents.json (or XQO_AUTH) turns it on; then every page and API call needs a signed
session cookie or `Authorization: Bearer <XQO_API_TOKEN>`. Only identities on the allowlist get in. A signed-in person
can link their other account (Google <-> Telegram), so both sign in as the same person. See docs/AUTH.md.

State lives in <data>/auth/ (next to artifacts/, never inside it, so no artifact route can serve it):
`secret` (cookie signing key, unless XQO_SESSION_SECRET is set), `links.json` (linked accounts) and `audit.log`."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

import httpx

log = logging.getLogger("xqo.auth")

PROVIDERS = ("google", "telegram", "token")
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_ISSUERS = {"https://accounts.google.com", "accounts.google.com"}
TELEGRAM_API = "https://api.telegram.org"
TOKEN_IDENTITY = "token:api"
FLOW_SECONDS = 600            # a Google sign-in must finish within 10 minutes
TELEGRAM_CODE_SECONDS = 180   # a Telegram link must be approved within 3 minutes
TELEGRAM_MAX_PENDING = 50
TOKEN_FAILS = 10              # wrong API tokens per client per 10 minutes before 429
ENTRY = re.compile(r"^(tg:\d{1,20}|google:[^@\s]+@[^@\s]+|\*@[^@\s]+\.[^@\s]+|[^@\s:*]+@[^@\s]+)$")
LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _env_list(v: str) -> list[str]:
    return [x.strip() for x in v.split(",") if x.strip()]


def safe_next(n: str | None) -> str:
    """Only a path on this server: no scheme, no host, no protocol-relative or backslash tricks."""
    n = (n or "/").strip()
    if not n.startswith("/") or n.startswith("//") or "\\" in n or any(ord(c) < 32 for c in n):
        return "/"
    return n


@dataclass
class AuthConfig:
    providers: list[str] = field(default_factory=list)  # empty: login is off
    allow: list[str] = field(default_factory=list)       # "me@gmail.com", "*@company.com", "tg:123456789"
    public_url: str = ""                                  # https://hq.example.com: redirect URI, cookie Secure flag
    session_hours: float = 168.0
    linking: bool = True                                  # signed-in people may link their other provider
    public_paths: list[str] = field(default_factory=list)  # extra path prefixes that need no login
    api_token_env: str = "XQO_API_TOKEN"
    google_client_id_env: str = "XQO_GOOGLE_CLIENT_ID"
    google_client_secret_env: str = "XQO_GOOGLE_CLIENT_SECRET"
    telegram_bot_token_env: str = "XQO_TELEGRAM_LOGIN_BOT_TOKEN"
    telegram_bot_username: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.providers)

    @classmethod
    def from_dict(cls, d: dict | None, env: dict | None = None) -> "AuthConfig":
        env = os.environ if env is None else env
        d = dict(d or {})
        known = {"mode", "allow", "public_url", "session_hours", "linking", "public_paths", "api_token_env",
                 "google", "telegram"}
        bad = set(d) - known
        if bad:
            raise ValueError(f"auth: unknown setting(s) {', '.join(sorted(bad))}")
        g, t = dict(d.get("google") or {}), dict(d.get("telegram") or {})
        if set(g) - {"client_id_env", "client_secret_env"}:
            raise ValueError("auth.google: known settings are client_id_env and client_secret_env")
        if set(t) - {"bot_token_env", "bot_username"}:
            raise ValueError("auth.telegram: known settings are bot_token_env and bot_username")
        mode = env.get("XQO_AUTH") or d.get("mode", "off")  # an empty variable (docker compose) = not set
        names = _env_list(mode) if isinstance(mode, str) else list(mode or [])
        if names in (["off"], ["none"], ["0"], ["false"], []):
            names = []
        elif names == ["any"]:
            names = ["google", "telegram"]
        if any(n not in PROVIDERS for n in names):
            raise ValueError(f"auth.mode: off, any, or a list of {', '.join(PROVIDERS)} (got {mode!r})")
        allow = _env_list(env["XQO_AUTH_ALLOW"]) if env.get("XQO_AUTH_ALLOW") else list(d.get("allow") or [])
        allow = [str(a).strip().lower() for a in allow]
        wrong = [a for a in allow if not ENTRY.match(a)]
        if wrong:
            raise ValueError(f"auth.allow: {', '.join(wrong)}: use an email, *@domain or tg:<numeric user id>")
        public_url = str(env.get("XQO_PUBLIC_URL") or d.get("public_url") or "").rstrip("/")
        if public_url:
            p = urlsplit(public_url)
            if p.scheme not in ("http", "https") or not p.netloc or p.path or p.query or p.username:
                raise ValueError("auth.public_url must be an origin like https://hq.example.com (no path)")
        hours = d.get("session_hours", 168)
        if not isinstance(hours, (int, float)) or not 0 < hours <= 24 * 365:
            raise ValueError("auth.session_hours must be a number of hours (up to a year)")
        if not isinstance(d.get("linking", True), bool):
            raise ValueError("auth.linking must be true or false")
        pub = d.get("public_paths") or []
        if not isinstance(pub, list) or not all(isinstance(x, str) and x.startswith("/") and len(x) > 1 for x in pub):
            raise ValueError("auth.public_paths must be a list of path prefixes like /hooks/")
        c = cls(list(dict.fromkeys(names)), allow, public_url, float(hours), d.get("linking", True), pub,
                str(d.get("api_token_env") or "XQO_API_TOKEN"),
                str(g.get("client_id_env") or "XQO_GOOGLE_CLIENT_ID"),
                str(g.get("client_secret_env") or "XQO_GOOGLE_CLIENT_SECRET"),
                str(t.get("bot_token_env") or "XQO_TELEGRAM_LOGIN_BOT_TOKEN"),
                str(t.get("bot_username") or "").lstrip("@"))
        if c.enabled:
            c.check(env)
        return c

    def check(self, env) -> None:
        """Refuse a configuration where login is on but nobody could sign in, or a provider lacks its secrets."""
        missing = []
        if "google" in self.providers:
            missing += [v for v in (self.google_client_id_env, self.google_client_secret_env) if not env.get(v)]
        if "telegram" in self.providers and not env.get(self.telegram_bot_token_env):
            missing.append(self.telegram_bot_token_env)
        if "token" in self.providers and not env.get(self.api_token_env):
            missing.append(self.api_token_env)
        if missing:
            raise ValueError(f"auth: set the environment variable(s) {', '.join(missing)}")
        people = [p for p in self.providers if p != "token"]
        if people and not self.allow:
            raise ValueError("auth.allow is empty: nobody could sign in (add emails, *@domain or tg:<id>)")


class Signer:
    """HMAC-SHA256 signed, expiring JSON values for cookies. `purpose` keeps one kind from passing as another."""

    def __init__(self, key: bytes):
        self.key = key

    def dumps(self, obj: dict, purpose: str, seconds: float) -> str:
        body = _b64(json.dumps({**obj, "exp": int(time.time() + seconds)}, separators=(",", ":")).encode())
        mac = hmac.new(self.key, f"{purpose}.{body}".encode(), hashlib.sha256).digest()
        return f"{body}.{_b64(mac)}"

    def loads(self, value: str | None, purpose: str) -> dict | None:
        if not value or value.count(".") != 1:
            return None
        body, mac = value.split(".")
        good = hmac.new(self.key, f"{purpose}.{body}".encode(), hashlib.sha256).digest()
        try:
            if not hmac.compare_digest(_unb64(mac), good):
                return None
            obj = json.loads(_unb64(body))
        except (ValueError, TypeError):
            return None
        if not isinstance(obj, dict) or not isinstance(obj.get("exp"), int) or obj["exp"] < time.time():
            return None
        return obj


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


class Links:
    """Linked accounts: identity -> person (the identity that signed in first), plus display names and the time
    before which every session is void ("sign out everywhere")."""

    def __init__(self, path: Path):
        self.path = path
        self.links: dict[str, str] = {}
        self.names: dict[str, str] = {}
        self.revoked_before = 0.0
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            self.links = {str(k): str(v) for k, v in (d.get("links") or {}).items()}
            self.names = {str(k): str(v) for k, v in (d.get("names") or {}).items()}
            self.revoked_before = float(d.get("revoked_before") or 0)
        except FileNotFoundError:
            pass
        except (ValueError, OSError, AttributeError) as e:
            raise ValueError(f"auth: {path} is unreadable ({e}); fix or remove it") from None

    def save(self) -> None:
        _write_private(self.path, json.dumps({"links": self.links, "names": self.names,
                                              "revoked_before": self.revoked_before}, indent=1))

    def person(self, identity: str) -> str:
        return self.links.get(identity, identity)

    def identities(self, person: str) -> list[str]:
        return [person] + sorted(i for i, p in self.links.items() if p == person)


@dataclass
class User:
    person: str      # the identity that stands for this person ("google:me@gmail.com")
    via: str         # the identity this session signed in with
    name: str
    by_token: bool = False  # Authorization header (CLI, scripts): no CSRF check


def provider_of(identity: str) -> str:
    p = identity.split(":", 1)[0]
    return "telegram" if p == "tg" else p


class Auth:
    def __init__(self, cfg: AuthConfig, folder: Path, hq_name: str = "xqo", env: dict | None = None):
        self.cfg, self.folder, self.hq_name = cfg, Path(folder), hq_name
        self.env = os.environ if env is None else env
        self.links = Links(self.folder / "links.json") if cfg.enabled else Links(Path("/nonexistent/links.json"))
        self.signer = Signer(self._secret()) if cfg.enabled else Signer(b"")
        tag = hashlib.sha256(self.signer.key + b"cookie").hexdigest()[:8]  # several HQs on one host: own cookies
        self.cookie, self.flow_cookie, self.tg_cookie = f"xqo_s_{tag}", f"xqo_f_{tag}", f"xqo_t_{tag}"
        self.secure = cfg.public_url.startswith("https://")
        self.telegram = TelegramLogin(self) if "telegram" in cfg.providers else None
        self._fails: dict[str, list[float]] = {}

    # ---- secrets and identities ------------------------------------------------------------------
    def _secret(self) -> bytes:
        if self.env.get("XQO_SESSION_SECRET"):
            return hashlib.sha256(self.env["XQO_SESSION_SECRET"].encode()).digest()
        p = self.folder / "secret"
        try:
            v = p.read_text(encoding="utf-8").strip()
            if len(v) >= 32:
                return v.encode()
        except FileNotFoundError:
            pass
        v = secrets.token_urlsafe(48)
        _write_private(p, v)
        return v.encode()

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    def api_token(self) -> str:
        return self.env.get(self.cfg.api_token_env) or ""

    def _match(self, identity: str) -> bool:
        ident = identity.lower()
        for e in self.cfg.allow:
            if e.startswith(("tg:", "google:")):
                if ident == e:
                    return True
            elif e.startswith("*@"):
                if ident.startswith("google:") and ident.endswith(e[1:]):
                    return True
            elif ident == "google:" + e:
                return True
        return False

    def allowed(self, identity: str) -> bool:
        if identity == TOKEN_IDENTITY:
            return bool(self.api_token())
        return any(self._match(i) for i in self.links.identities(self.links.person(identity)))

    def audit(self, what: str, **info) -> None:
        line = json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": what, **info})
        log.info("auth %s", line)
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            with open(self.folder / "audit.log", "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    # ---- sessions -------------------------------------------------------------------------------
    def session_value(self, identity: str, name: str) -> str:
        person = self.links.person(identity)
        return self.signer.dumps({"p": person, "v": identity, "n": name[:100], "iat": time.time()},
                                 "session", self.cfg.session_hours * 3600)

    def set_session(self, response, identity: str, name: str) -> None:
        response.set_cookie(self.cookie, self.session_value(identity, name), max_age=int(self.cfg.session_hours * 3600),
                            httponly=True, samesite="lax", secure=self.secure, path="/")

    def clear_session(self, response) -> None:
        response.delete_cookie(self.cookie, path="/", httponly=True, samesite="lax", secure=self.secure)

    def user_from(self, headers: dict[str, str], cookies: dict[str, str], client: str = "") -> User | None:
        authz = headers.get("authorization") or ""
        if authz.lower().startswith("bearer "):
            token, want = authz[7:].strip(), self.api_token()
            if want and token and hmac.compare_digest(token.encode(), want.encode()):
                return User(TOKEN_IDENTITY, TOKEN_IDENTITY, "API token", by_token=True)
            self.failed(client)
            return None
        s = self.signer.loads(cookies.get(self.cookie), "session")
        if not s:
            return None
        via, person = str(s.get("v", "")), str(s.get("p", ""))
        if float(s.get("iat") or 0) <= self.links.revoked_before:
            return None
        if self.links.person(via) != person or not self.allowed(via):  # unlinked, or taken off the allowlist
            return None
        return User(person, via, str(s.get("n") or via))

    def failed(self, client: str) -> None:
        now = time.time()
        self._fails[client] = [t for t in self._fails.get(client, []) if now - t < 600] + [now]
        if len(self._fails) > 10_000:  # bounded memory
            self._fails.clear()

    def throttled(self, client: str) -> bool:
        now = time.time()
        return len([t for t in self._fails.get(client, []) if now - t < 600]) >= TOKEN_FAILS

    def me(self, user: User | None) -> dict:
        out = {"enabled": self.enabled, "providers": [p for p in self.cfg.providers],
               "linking": self.cfg.linking, "user": None}
        if user:
            ids = self.links.identities(user.person) if user.person != TOKEN_IDENTITY else [TOKEN_IDENTITY]
            have = {provider_of(i) for i in ids}
            out["user"] = {"person": user.person, "via": user.via, "name": user.name,
                           "identities": [{"id": i, "provider": provider_of(i), "name": self.links.names.get(i, i),
                                           "primary": i == user.person} for i in ids],
                           "can_link": [p for p in self.cfg.providers if p in ("google", "telegram") and p not in have]
                           if self.cfg.linking and user.person != TOKEN_IDENTITY else []}
        return out

    def link(self, person: str, identity: str, name: str) -> str | None:
        """Link `identity` to `person`; an error message when it can't be."""
        if not self.cfg.linking:
            return "linking accounts is turned off (auth.linking)"
        if person == TOKEN_IDENTITY:
            return "the API token can't have linked accounts"
        if identity == person or self.links.links.get(identity) == person:
            self.links.names[identity] = name
            self.links.save()
            return None
        if identity in self.links.links or any(p == identity for p in self.links.links.values()):
            return f"{identity} is already linked to another sign-in; unlink it there first"
        self.links.links[identity] = person
        self.links.names[identity] = name
        self.links.save()
        self.audit("link", person=person, identity=identity)
        return None

    def unlink(self, person: str, identity: str) -> str | None:
        if self.links.links.get(identity) != person:
            return "only a linked account of yours can be unlinked (not the one you first signed in with)"
        del self.links.links[identity]
        self.links.save()
        self.audit("unlink", person=person, identity=identity)
        return None

    def revoke_all(self) -> None:
        self.links.revoked_before = time.time()
        self.links.save()
        self.audit("revoke_all")

    # ---- request checks ---------------------------------------------------------------------------
    def is_public(self, path: str, method: str) -> bool:
        if path in ("/login", "/api/auth/me") or path.startswith("/auth/"):
            return True
        if re.fullmatch(r"/api/channels/[^/]+/webhook", path):  # each channel checks its own secret
            return True
        if method == "POST" and re.fullmatch(r"/api/feeds/[^/]+/events", path):  # push feeds: token or signature
            return True
        return any(path.startswith(p) for p in self.cfg.public_paths)

    def same_origin(self, headers: dict[str, str], scheme: str) -> bool:
        """For cookie-signed-in requests that change something: the browser's Origin (or Referer) is this server."""
        origin = headers.get("origin")
        if not origin or origin == "null":
            ref = headers.get("referer")
            if not ref:
                return False
            p = urlsplit(ref)
            origin = f"{p.scheme}://{p.netloc}"
        host = headers.get("host", "")
        ok = {f"{scheme}://{host}", f"https://{host}", f"http://{host}"} if host else set()
        if self.cfg.public_url:
            ok.add(self.cfg.public_url)
        return origin.rstrip("/") in ok

    def base_url(self, scheme: str, host: str) -> str | None:
        """Where the provider sends the browser back to: public_url, else this host when it is the local machine."""
        if self.cfg.public_url:
            return self.cfg.public_url
        name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
        if name in LOOPBACK:
            return f"{scheme}://{host}"
        return None

    # ---- Google (OpenID Connect, authorization code + PKCE) ----------------------------------------
    def google_start(self, base: str, next_path: str, link_person: str | None) -> tuple[str, str]:
        state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
        flow = self.signer.dumps({"s": state, "n": nonce, "v": verifier, "next": safe_next(next_path),
                                  "link": link_person or "", "r": base}, "google-flow", FLOW_SECONDS)
        challenge = _b64(hashlib.sha256(verifier.encode()).digest())
        q = {"client_id": self.env[self.cfg.google_client_id_env], "redirect_uri": f"{base}/auth/google/callback",
             "response_type": "code", "scope": "openid email profile", "state": state, "nonce": nonce,
             "code_challenge": challenge, "code_challenge_method": "S256", "prompt": "select_account"}
        return f"{GOOGLE_AUTH}?{urlencode(q)}", flow

    async def google_token(self, data: dict) -> dict:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(GOOGLE_TOKEN, data=data)
        if r.status_code != 200:
            raise ValueError(f"Google refused the sign-in code (HTTP {r.status_code})")
        return r.json()

    async def google_finish(self, flow_value: str | None, code: str, state: str) -> tuple[dict, str, str]:
        """(flow, identity, name). The ID token comes straight from Google's token endpoint over TLS, which
        OpenID Connect Core 3.1.3.7 accepts in place of checking its signature; its claims are all checked."""
        flow = self.signer.loads(flow_value, "google-flow")
        if not flow or not state or not hmac.compare_digest(str(flow.get("s")), state):
            raise ValueError("the sign-in expired or came from another browser; try again")
        tok = await self.google_token({
            "code": code, "client_id": self.env[self.cfg.google_client_id_env],
            "client_secret": self.env[self.cfg.google_client_secret_env],
            "redirect_uri": f"{flow['r']}/auth/google/callback", "grant_type": "authorization_code",
            "code_verifier": flow["v"]})
        try:
            claims = json.loads(_unb64(str(tok["id_token"]).split(".")[1]))
        except (KeyError, IndexError, ValueError, TypeError):
            raise ValueError("Google sent no usable ID token") from None
        cid = self.env[self.cfg.google_client_id_env]
        aud = claims.get("aud")
        if claims.get("iss") not in GOOGLE_ISSUERS:
            raise ValueError("ID token: wrong issuer")
        if (aud != cid) if isinstance(aud, str) else (not isinstance(aud, list) or cid not in aud):
            raise ValueError("ID token: issued for another client")
        if claims.get("azp") not in (None, cid):
            raise ValueError("ID token: wrong authorized party")
        if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] < time.time() - 60:
            raise ValueError("ID token: expired")
        if not flow.get("n") or not hmac.compare_digest(str(claims.get("nonce", "")), str(flow["n"])):
            raise ValueError("ID token: wrong nonce")
        email = str(claims.get("email") or "").lower()
        if not email or claims.get("email_verified") not in (True, "true"):
            raise ValueError("this Google account has no verified email address")
        return flow, f"google:{email}", str(claims.get("name") or email)


class TelegramLogin:
    """Sign in by opening t.me/<bot>?start=<code> and pressing Approve in Telegram.

    The browser that asked for the code holds it in a signed cookie, so only it can collect the session. The bot
    long-polls Telegram only while a code is waiting, so no public URL is needed. Use a bot of its own: Telegram
    gives updates to one reader per bot, so it can't share a bot that xqo-channels polls."""

    def __init__(self, auth: Auth):
        self.auth = auth
        self.pending: dict[str, dict] = {}
        self.offset = 0
        self.username = auth.cfg.telegram_bot_username
        self.task: asyncio.Task | None = None
        self.error = ""

    def token(self) -> str:
        return self.auth.env.get(self.auth.cfg.telegram_bot_token_env) or ""

    async def call(self, method: str, **params) -> dict | list | bool:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10, read=40)) as c:
            r = await c.post(f"{TELEGRAM_API}/bot{self.token()}/{method}", json=params)
        d = r.json()
        if not d.get("ok"):
            raise RuntimeError(f"Telegram {method}: {d.get('description') or r.status_code}")
        return d["result"]

    def _sweep(self) -> None:
        now = time.time()
        for c in [c for c, p in self.pending.items() if p["exp"] < now - 60]:
            del self.pending[c]

    async def start(self, next_path: str, link_person: str | None, client: str, agent: str) -> tuple[dict, str]:
        self._sweep()
        if len([p for p in self.pending.values() if p["status"] == "pending"]) >= TELEGRAM_MAX_PENDING:
            raise RuntimeError("too many Telegram sign-ins waiting; try again in a few minutes")
        if not self.username:
            me = await self.call("getMe")
            self.username = str(me.get("username") or "")
        code, bind = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
        self.pending[code] = {"exp": time.time() + TELEGRAM_CODE_SECONDS, "bind": bind, "next": safe_next(next_path),
                              "link": link_person or "", "status": "pending", "client": client[:60],
                              "agent": agent[:80], "identity": "", "name": "", "from": None, "reason": ""}
        cookie = self.auth.signer.dumps({"c": code, "b": bind}, "telegram-flow", TELEGRAM_CODE_SECONDS + 30)
        self.ensure_polling()
        return {"url": f"https://t.me/{self.username}?start={code}", "expires_in": TELEGRAM_CODE_SECONDS}, cookie

    def ensure_polling(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._poll())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass

    async def _poll(self) -> None:
        while any(p["status"] == "pending" and p["exp"] > time.time() for p in self.pending.values()):
            try:
                updates = await self.call("getUpdates", offset=self.offset, timeout=25,
                                          allowed_updates=["message", "callback_query"])
                self.error = ""
            except asyncio.CancelledError:
                raise
            except Exception as e:  # network trouble or a webhook set on this bot: shown on the login page
                self.error = str(e)[:200]
                await asyncio.sleep(3)
                continue
            for u in updates or []:
                self.offset = max(self.offset, int(u.get("update_id", 0)) + 1)
                try:
                    await self.handle(u)
                except Exception as e:  # one bad update never stops the others
                    log.warning("telegram login update failed: %s", e)

    async def handle(self, u: dict) -> None:
        if "message" in u:
            m = u["message"]
            chat, frm, text = m.get("chat") or {}, m.get("from") or {}, str(m.get("text") or "")
            if chat.get("type") != "private" or not frm.get("id"):
                return
            if not text.startswith("/start"):
                await self.call("sendMessage", chat_id=chat["id"], text=f"This bot signs you in to {self.auth.hq_name}. "
                                "Use 'Sign in with Telegram' on the dashboard.")
                return
            code = text[6:].strip()
            p = self.pending.get(code)
            if not p or p["status"] != "pending" or p["exp"] < time.time():
                await self.call("sendMessage", chat_id=chat["id"],
                                text="This sign-in link has expired. Start again from the dashboard.")
                return
            if p["from"] not in (None, frm["id"]):
                return  # someone else already opened this link: only they can approve or deny it
            p["from"] = frm["id"]
            what = "Link this Telegram account to your sign-in on" if p["link"] else "Sign in to"
            await self.call("sendMessage", chat_id=chat["id"],
                            text=f"{what} {self.auth.hq_name}?\n\nRequested from {p['client'] or 'an unknown address'}"
                                 f" ({p['agent'] or 'unknown browser'}). Approve only if this was you, just now.",
                            reply_markup={"inline_keyboard": [[{"text": "Approve", "callback_data": f"ok:{code}"},
                                                               {"text": "Deny", "callback_data": f"no:{code}"}]]})
        elif "callback_query" in u:
            q = u["callback_query"]
            frm, data = q.get("from") or {}, str(q.get("data") or "")
            verb, _, code = data.partition(":")
            p = self.pending.get(code)
            answer = "This sign-in has expired."
            if p and p["status"] == "pending" and p["exp"] >= time.time() and p["from"] == frm.get("id"):
                identity = f"tg:{int(frm['id'])}"
                name = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x) or \
                       (f"@{frm['username']}" if frm.get("username") else identity)
                if verb != "ok":
                    p["status"], answer = "denied", "Denied. Nobody was signed in."
                elif not p["link"] and not self.auth.allowed(identity):
                    p["status"], p["reason"] = "denied", f"{identity} is not on the allowlist (auth.allow)"
                    answer = f"This Telegram account is not allowed. Ask the owner to add {identity} to auth.allow."
                else:
                    p.update(status="approved", identity=identity, name=name)
                    answer = "Linked." if p["link"] else f"Signed in to {self.auth.hq_name}."
            await self.call("answerCallbackQuery", callback_query_id=q.get("id"), text=answer[:190])
            msg = q.get("message") or {}
            if msg.get("chat", {}).get("id") and msg.get("message_id"):
                await self.call("editMessageText", chat_id=msg["chat"]["id"], message_id=msg["message_id"], text=answer)

    def collect(self, cookie_value: str | None) -> dict:
        """The browser asks how its code is doing; once approved, it gets the result exactly once."""
        c = self.auth.signer.loads(cookie_value, "telegram-flow")
        p = self.pending.get(str(c.get("c"))) if c else None
        if not c or not p or not hmac.compare_digest(p["bind"], str(c.get("b", ""))):
            return {"status": "expired"}
        if p["status"] == "pending" and p["exp"] < time.time():
            p["status"] = "expired"
        if p["status"] == "pending":
            self.ensure_polling()
            return {"status": "pending", "error": self.error}
        if p["status"] == "approved":
            del self.pending[c["c"]]
            return {"status": "approved", "identity": p["identity"], "name": p["name"], "next": p["next"],
                    "link": p["link"]}
        return {"status": p["status"], "reason": p["reason"]}


def login_url(path: str, query: str = "") -> str:
    full = path + (f"?{query}" if query else "")
    return "/login" + (f"?next={quote(full, safe='')}" if full not in ("", "/") else "")
