"""The dashboard side of auth.py: the middleware that guards every route, and the /login and /auth/* routes."""
from __future__ import annotations

import hmac
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from starlette.requests import HTTPConnection

from .auth import SAFE_METHODS, TOKEN_IDENTITY, Auth, User, login_url, safe_next


def _client(conn: HTTPConnection) -> str:
    return conn.client.host if conn.client else ""


def current_user(conn: HTTPConnection) -> User | None:
    return conn.scope.get("xqo_user")


class AuthMiddleware:
    """Pure ASGI (keeps the SSE stream at /api/events streaming). Off: a pass-through."""

    def __init__(self, app, auth: Auth):
        self.app, self.auth = app, auth

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket") or not self.auth.enabled:
            return await self.app(scope, receive, send)
        conn = HTTPConnection(scope)
        path, method = scope["path"], scope.get("method", "GET")
        headers = {k: v for k, v in conn.headers.items()}
        if headers.get("authorization") and self.auth.throttled(_client(conn)):
            return await JSONResponse({"detail": "too many wrong tokens; wait 10 minutes"}, 429)(scope, receive, send)
        user = self.auth.user_from(headers, conn.cookies, _client(conn))
        scope["xqo_user"] = user
        public = self.auth.is_public(path, method)
        hook = public and not (path.startswith("/auth/") or path in ("/login", "/api/auth/me"))
        if method not in SAFE_METHODS and not hook and not (user and user.by_token) \
                and not self.auth.same_origin(headers, scope.get("scheme", "http")):
            return await JSONResponse({"detail": "cross-site request refused (Origin does not match this server)"},
                                      403)(scope, receive, send)
        if user or public:
            return await self.app(scope, receive, send)
        if path == "/api/health":  # Docker's health check still works; the details need a sign-in
            return await JSONResponse({"ok": True, "auth": "required"})(scope, receive, send)
        html = method == "GET" and "text/html" in headers.get("accept", "") and not path.startswith("/api/")
        if html:
            q = scope.get("query_string", b"").decode("latin-1")
            return await RedirectResponse(login_url(path, q), 303)(scope, receive, send)
        return await JSONResponse({"detail": "sign in required", "login": "/login"}, 401,
                                  headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)


class TelegramStartIn(BaseModel):
    next: str = Field(default="/", max_length=2000)
    link: bool = False


class TokenIn(BaseModel):
    token: str = Field(min_length=1, max_length=500)
    next: str = Field(default="/", max_length=2000)


class LogoutIn(BaseModel):
    everywhere: bool = False


class UnlinkIn(BaseModel):
    identity: str = Field(min_length=3, max_length=300)


def _login_error(msg: str, next_path: str = "/") -> RedirectResponse:
    return RedirectResponse("/login?" + urlencode({"error": msg[:300], "next": safe_next(next_path)}), 303)


def install(app: FastAPI, auth: Auth, page) -> None:
    """Register the middleware and routes. `page(name)` renders a static page with the HQ name."""
    app.add_middleware(AuthMiddleware, auth=auth)

    @app.get("/api/auth/me")
    async def auth_me(request: Request):
        return auth.me(current_user(request))

    if not auth.enabled:
        return

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, next: str = "/"):
        if current_user(request):
            return RedirectResponse(safe_next(next), 303)
        return page("login.html")

    @app.post("/auth/logout")
    async def logout(request: Request, body: LogoutIn | None = None):
        user = current_user(request)
        if body and body.everywhere and user:
            auth.revoke_all()
        if user:
            auth.audit("logout", person=user.person, everywhere=bool(body and body.everywhere))
        r = JSONResponse({"ok": True})
        auth.clear_session(r)
        return r

    @app.post("/api/auth/unlink")
    async def unlink(body: UnlinkIn, request: Request):
        user = current_user(request)
        err = auth.unlink(user.person, body.identity)
        if err:
            raise HTTPException(400, err)
        return auth.me(user)

    async def signed_in(request: Request, identity: str, name: str, next_path: str, link_person: str,
                        how: str) -> tuple[str | None, str]:
        """Link or sign in; (error, where to go)."""
        if link_person:
            user = current_user(request)
            if not user or user.person != link_person:
                return "sign in again before linking an account", next_path
            err = auth.link(link_person, identity, name)
            return err, next_path
        if not auth.allowed(identity):
            auth.audit("refused", identity=identity, via=how, client=_client(request))
            return f"{identity.split(':', 1)[1]} is not allowed to sign in here (auth.allow)", next_path
        auth.links.names.setdefault(identity, name)
        auth.audit("login", identity=identity, person=auth.links.person(identity), via=how, client=_client(request))
        return None, next_path

    if "token" in auth.cfg.providers:
        @app.post("/auth/token")
        async def token_login(body: TokenIn, request: Request):
            client = _client(request)
            if auth.throttled(client):
                raise HTTPException(429, "too many wrong tokens; wait 10 minutes")
            want = auth.api_token()
            if not want or not hmac.compare_digest(body.token.encode(), want.encode()):
                auth.failed(client)
                auth.audit("refused", via="token", client=client)
                raise HTTPException(403, "wrong token")
            auth.audit("login", identity=TOKEN_IDENTITY, via="token", client=client)
            r = JSONResponse({"ok": True, "next": safe_next(body.next)})
            auth.set_session(r, TOKEN_IDENTITY, "API token")
            return r

    if "google" in auth.cfg.providers:
        @app.get("/auth/google/start")
        async def google_start(request: Request, next: str = "/", link: bool = False):
            base = auth.base_url(request.url.scheme, request.headers.get("host", ""))
            if not base:
                return _login_error("set auth.public_url (or XQO_PUBLIC_URL) to sign in with Google from "
                                    "another machine", next)
            user = current_user(request)
            if link and not user:
                return _login_error("sign in first, then link your Google account", next)
            url, flow = auth.google_start(base, next, user.person if link and user else None)
            r = RedirectResponse(url, 303)
            r.set_cookie(auth.flow_cookie, flow, max_age=600, httponly=True, samesite="lax", secure=auth.secure,
                         path="/auth/google/")
            return r

        @app.get("/auth/google/callback")
        async def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
            if error:
                return _login_error(f"Google sign-in was cancelled ({error})")
            try:
                flow, identity, name = await auth.google_finish(request.cookies.get(auth.flow_cookie), code, state)
            except (ValueError, KeyError) as e:
                return _login_error(str(e))
            except Exception as e:  # network trouble reaching Google
                return _login_error(f"could not reach Google: {type(e).__name__}")
            err, nxt = await signed_in(request, identity, name, flow["next"], flow.get("link") or "", "google")
            if err:
                r = _login_error(err, nxt) if not flow.get("link") else RedirectResponse(
                    "/?" + urlencode({"auth_error": err}), 303)
            else:
                r = RedirectResponse(nxt, 303)
                if not flow.get("link"):
                    auth.set_session(r, identity, name)
            r.delete_cookie(auth.flow_cookie, path="/auth/google/")
            return r

    if auth.telegram:
        tg = auth.telegram

        @app.post("/auth/telegram/start")
        async def telegram_start(body: TelegramStartIn, request: Request):
            user = current_user(request)
            if body.link and not user:
                raise HTTPException(401, "sign in first, then link your Telegram account")
            try:
                out, cookie = await tg.start(body.next, user.person if body.link and user else None,
                                             _client(request), request.headers.get("user-agent", ""))
            except Exception as e:
                raise HTTPException(503, f"Telegram is not reachable: {e}")
            r = JSONResponse(out)
            r.set_cookie(auth.tg_cookie, cookie, max_age=out["expires_in"] + 30, httponly=True, samesite="lax",
                         secure=auth.secure, path="/auth/telegram/")
            return r

        @app.get("/auth/telegram/status")
        async def telegram_status(request: Request):
            res = tg.collect(request.cookies.get(auth.tg_cookie))
            if res["status"] != "approved":
                return res
            err, nxt = await signed_in(request, res["identity"], res["name"], res["next"], res["link"], "telegram")
            if err:
                r = JSONResponse({"status": "denied", "reason": err})
            else:
                r = JSONResponse({"status": "approved", "next": nxt, "linked": bool(res["link"])})
                if not res["link"]:
                    auth.set_session(r, res["identity"], res["name"])
            r.delete_cookie(auth.tg_cookie, path="/auth/telegram/")
            return r

