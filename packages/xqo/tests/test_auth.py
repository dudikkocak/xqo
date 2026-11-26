"""Optional login (auth.py, auth_web.py): off by default; Google, Telegram and the API token; linking; CSRF."""
import asyncio
import base64
import json
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from xqo.auth import AuthConfig, Signer, safe_next
from xqo.config import build
from xqo.web import create_app

ROOT = Path(__file__).resolve().parent.parent
ORIGIN = {"Origin": "http://testserver"}
ENV = {"XQO_GOOGLE_CLIENT_ID": "cid.apps.googleusercontent.com", "XQO_GOOGLE_CLIENT_SECRET": "gsecret",
       "XQO_TELEGRAM_LOGIN_BOT_TOKEN": "123:abc", "XQO_API_TOKEN": "tok-" + "x" * 30}


def make(tmp_path, monkeypatch, auth: dict | None):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    for k in ("XQO_AUTH", "XQO_AUTH_ALLOW", "XQO_PUBLIC_URL", "XQO_SESSION_SECRET"):
        monkeypatch.delenv(k, raising=False)
    cfg = json.loads((ROOT / "agents.json").read_text())
    if auth is not None:
        cfg["auth"] = auth
    p = tmp_path / "agents.json"
    p.write_text(json.dumps(cfg))
    bus = build(p, tmp_path / "data" / "artifacts", str(tmp_path / "data" / "ev.jsonl"), mock=True)
    return bus, TestClient(create_app(bus))


ON = {"mode": ["google", "telegram", "token"], "allow": ["me@gmail.com", "tg:42"], "public_url": "http://testserver"}


def id_token(**claims) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"eyJhbGciOiJSUzI1NiJ9.{body}.sig"


def google_login(bus, client, email="me@gmail.com", next_path="/", link=False, **over):
    r = client.get(f"/auth/google/start?next={next_path}" + ("&link=1" if link else ""), follow_redirects=False)
    assert r.status_code == 303, r.text
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert q["redirect_uri"] == ["http://testserver/auth/google/callback"]
    assert q["code_challenge_method"] == ["S256"] and q["scope"] == ["openid email profile"]
    claims = {"iss": "https://accounts.google.com", "aud": ENV["XQO_GOOGLE_CLIENT_ID"], "exp": time.time() + 600,
              "nonce": q["nonce"][0], "email": email, "email_verified": True, "name": "Me", **over}

    async def token(data):
        assert data["code_verifier"] and data["code"] == "c0de"
        return {"id_token": id_token(**claims)}
    bus.auth.google_token = token
    return client.get(f"/auth/google/callback?code=c0de&state={q['state'][0]}", follow_redirects=False)


class FakeBot:
    def __init__(self):
        self.sent = []

    async def __call__(self, method, **params):
        self.sent.append((method, params))
        if method == "getUpdates":
            await asyncio.sleep(0.05)
            return []
        if method == "getMe":
            return {"username": "hq_login_bot"}
        return True


def telegram_approve(bus, code, user_id, verb="ok"):
    tg = bus.auth.telegram
    asyncio.run(tg.handle({"message": {"chat": {"id": user_id, "type": "private"}, "from": {"id": user_id,
                                                                                         "first_name": "Tee"},
                                       "text": f"/start {code}"}}))
    asyncio.run(tg.handle({"callback_query": {"id": "q1", "from": {"id": user_id, "first_name": "Tee"},
                                              "data": f"{verb}:{code}",
                                              "message": {"chat": {"id": user_id}, "message_id": 7}}}))


def telegram_start(bus, client, link=False):
    bus.auth.telegram.call = FakeBot()
    r = client.post("/auth/telegram/start", json={"next": "/office", "link": link}, headers=ORIGIN)
    assert r.status_code == 200, r.text
    assert r.json()["url"].startswith("https://t.me/hq_login_bot?start=")
    return r.json()["url"].split("start=")[1]


# ---- off by default ----------------------------------------------------------------------------------
def test_off_by_default_changes_nothing(tmp_path, monkeypatch):
    bus, c = make(tmp_path, monkeypatch, None)
    assert not bus.auth.enabled
    assert c.get("/api/tasks").status_code == 200
    assert c.get("/api/auth/me").json() == {"enabled": False, "providers": [], "linking": True, "user": None}
    assert c.get("/login").status_code == 404
    assert c.post("/api/alerts/ack", json={}).status_code == 200  # no Origin needed when off
    assert not (tmp_path / "data" / "auth").exists()  # no secret written


def test_env_turns_it_on_and_off(tmp_path, monkeypatch):
    monkeypatch.setenv("XQO_AUTH", "off")
    c = AuthConfig.from_dict(ON, {**ENV, "XQO_AUTH": "off"})
    assert not c.enabled
    c = AuthConfig.from_dict({}, {**ENV, "XQO_AUTH": "google", "XQO_AUTH_ALLOW": "a@b.com, *@corp.io"})
    assert c.providers == ["google"] and c.allow == ["a@b.com", "*@corp.io"]
    assert AuthConfig.from_dict({"mode": "any", "allow": ["tg:1"]}, ENV).providers == ["google", "telegram"]
    assert AuthConfig.from_dict({"mode": "google", "allow": ["a@b.com"]}, {**ENV, "XQO_AUTH": ""}).enabled  # empty = unset


@pytest.mark.parametrize("cfg,env,msg", [
    ({"mode": "google", "allow": []}, ENV, "nobody could sign in"),
    ({"mode": "google", "allow": ["a@b.com"]}, {}, "XQO_GOOGLE_CLIENT_ID"),
    ({"mode": "telegram", "allow": ["tg:1"]}, {}, "XQO_TELEGRAM_LOGIN_BOT_TOKEN"),
    ({"mode": "google", "allow": ["bob"]}, ENV, "auth.allow"),
    ({"mode": "google", "allow": ["tg:bob"]}, ENV, "auth.allow"),
    ({"mode": "facebook"}, ENV, "auth.mode"),
    ({"mode": "google", "allow": ["a@b.com"], "public_url": "https://x.io/hq"}, ENV, "public_url"),
    ({"mode": "google", "allow": ["a@b.com"], "secret": "x"}, ENV, "unknown"),
])
def test_config_errors(cfg, env, msg):
    with pytest.raises(ValueError, match=msg):
        AuthConfig.from_dict(cfg, env)


def test_token_only_needs_no_allowlist():
    assert AuthConfig.from_dict({"mode": "token"}, ENV).providers == ["token"]


# ---- the gate --------------------------------------------------------------------------------------------
def test_everything_needs_a_sign_in(tmp_path, monkeypatch):
    bus, c = make(tmp_path, monkeypatch, ON)
    r = c.get("/api/tasks")
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    r = c.get("/office?x=1", headers={"Accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2Foffice%3Fx%3D1"
    assert c.get("/api/events").status_code == 401
    assert c.get("/static/app.css").status_code == 401
    assert c.get("/api/health").json() == {"ok": True, "auth": "required"}  # Docker health check, no details
    assert c.get("/login").status_code == 200 and "Sign in" in c.get("/login").text
    assert c.get("/api/auth/me").json()["user"] is None
    assert c.post("/api/channels/x/webhook").status_code != 401  # channels check their own secret
    assert c.get("/api/feeds/x/events").status_code == 401  # reading feed events needs a sign-in; pushing doesn't
    secret = tmp_path / "data" / "auth" / "secret"
    assert secret.is_file() and oct(secret.stat().st_mode & 0o777) == "0o600"
    assert c.get("/api/artifacts/file/../auth/secret").status_code in (401, 404)


def test_api_token_and_throttle(tmp_path, monkeypatch):
    bus, c = make(tmp_path, monkeypatch, ON)
    good = {"Authorization": f"Bearer {ENV['XQO_API_TOKEN']}"}
    assert c.get("/api/tasks", headers=good).status_code == 200
    assert c.post("/api/alerts/ack", json={}, headers=good).status_code == 200  # no Origin needed with a token
    assert c.get("/api/health", headers=good).json()["version"]
    for _ in range(10):
        assert c.get("/api/tasks", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.get("/api/tasks", headers={"Authorization": "Bearer nope"}).status_code == 429
    r = c.post("/auth/token", json={"token": ENV["XQO_API_TOKEN"]}, headers=ORIGIN)
    assert r.status_code == 429  # the browser token form shares the limit


def test_token_form_login(tmp_path, monkeypatch):
    bus, c = make(tmp_path, monkeypatch, ON)
    assert c.post("/auth/token", json={"token": "wrong"}, headers=ORIGIN).status_code == 403
    r = c.post("/auth/token", json={"token": ENV["XQO_API_TOKEN"], "next": "//evil.com"}, headers=ORIGIN)
    assert r.status_code == 200 and r.json()["next"] == "/"
    assert c.get("/api/tasks").status_code == 200


# ---- Google ----------------------------------------------------------------------------------------------
def test_google_sign_in(tmp_path, monkeypatch):
    bus, c = make(tmp_path, monkeypatch, ON)
    r = google_login(bus, c, next_path="/office")
    assert r.status_code == 303 and r.headers["location"] == "/office"
    assert c.get("/api/tasks").status_code == 200
    me = c.get("/api/auth/me").json()["user"]
    assert me["person"] == "google:me@gmail.com" and me["name"] == "Me" and me["can_link"] == ["telegram"]
    assert '"login"' in (tmp_path / "data" / "auth" / "audit.log").read_text()


def test_google_domain_allowlist_and_case(tmp_path, monkeypatch):
    bus, c = make(tmp_path, monkeypatch, {**ON, "allow": ["*@corp.io"]})
    assert google_login(bus, c, email="Ann@Corp.io").headers["location"] == "/"
    assert c.get("/api/auth/me").json()["user"]["person"] == "google:ann@corp.io"


@pytest.mark.parametrize("over,msg", [
    ({"email": "stranger@gmail.com"}, "not allowed"),
    ({"aud": "other-client"}, "another client"),
    ({"iss": "https://evil.example"}, "issuer"),
    ({"nonce": "replayed"}, "nonce"),
    ({"exp": 1}, "expired"),
    ({"email_verified": False}, "verified"),
])
def test_google_refusals(tmp_path, monkeypatch, over, msg):
    bus, c = make(tmp_path, monkeypatch, ON)
    r = google_login(bus, c, **over)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?error=")
    assert msg in parse_qs(urlsplit(r.headers["location"]).query)["error"][0]
    assert c.get("/api/tasks").status_code == 401


def test_google_state_must_match(tmp_path, monkeypatch):
    bus, c = make(tmp_path, monkeypatch, ON)
    c.get("/auth/google/start", follow_redirects=False)
    r = c.get("/auth/google/callback?code=x&state=forged", follow_redirects=False)
    assert "expired or came from another browser" in parse_qs(urlsplit(r.headers["location"]).query)["error"][0]


def test_google_needs_public_url_off_localhost(tmp_path, monkeypatch):
    bus, c = make(tmp_path, monkeypatch, {**ON, "public_url": ""})
    r = c.get("/auth/google/start", follow_redirects=False)  # testserver is not the local machine
    assert "public_url" in r.headers["location"]
    r = c.get("/auth/google/start", headers={"Host": "127.0.0.1:8000"}, follow_redirects=False)
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A8000%2Fauth%2Fgoogle%2Fcallback" in r.headers["location"]


# ---- CSRF and sessions -------------------------------------------------------------------------------------
def test_cookie_requests_need_same_origin(tmp_path, monkeypatch):
    bus, c = make(tmp_path, monkeypatch, ON)
    google_login(bus, c)
    assert c.post("/api/alerts/ack", json={}).status_code == 403  # no Origin, no Referer
    assert c.post("/api/alerts/ack", json={}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.post("/api/alerts/ack", json={}, headers=ORIGIN).status_code == 200
    assert c.post("/api/alerts/ack", json={}, headers={"Referer": "http://testserver/office"}).status_code == 200


def test_sessions_end(tmp_path, monkeypatch):
    bus, c = make(tmp_path, monkeypatch, ON)
    google_login(bus, c)
    assert c.get("/api/tasks").status_code == 200
    bus.auth.cfg.allow.remove("me@gmail.com")  # taken off the allowlist: the next request is refused
    assert c.get("/api/tasks").status_code == 401
    bus.auth.cfg.allow.append("me@gmail.com")
    assert c.get("/api/tasks").status_code == 200
    c2 = TestClient(c.app)
    google_login(bus, c2)
    assert c.post("/auth/logout", json={"everywhere": True}, headers=ORIGIN).status_code == 200
    assert c2.get("/api/tasks").status_code == 401  # every session, other browsers too
    google_login(bus, c2)
    assert c2.get("/api/tasks").status_code == 200


def test_signer():
    s = Signer(b"k" * 32)
    v = s.dumps({"a": 1}, "session", 60)
    assert s.loads(v, "session")["a"] == 1
    assert s.loads(v, "google-flow") is None  # one kind can't pass for another
    assert s.loads(v[:-2] + "xx", "session") is None
    assert s.loads(s.dumps({"a": 1}, "session", -1), "session") is None
    assert Signer(b"j" * 32).loads(v, "session") is None


def test_safe_next():
    for bad in ("//evil.com", "https://evil.com", "/\\evil.com", "evil", "/a\nb"):
        assert safe_next(bad) == "/"
    assert safe_next("/office?task=1") == "/office?task=1"


# ---- Telegram ------------------------------------------------------------------------------------------------
def test_telegram_sign_in(tmp_path, monkeypatch):
    bus, c = make(tmp_path, monkeypatch, ON)
    code = telegram_start(bus, c)
    assert c.get("/auth/telegram/status").json()["status"] == "pending"
    assert TestClient(c.app).get("/auth/telegram/status").json()["status"] == "expired"  # another browser: nothing
    telegram_approve(bus, code, 42)
    r = c.get("/auth/telegram/status")
    assert r.json() == {"status": "approved", "next": "/office", "linked": False}
    assert c.get("/api/tasks").status_code == 200
    assert c.get("/api/auth/me").json()["user"]["person"] == "tg:42"
    assert c.get("/auth/telegram/status").json()["status"] == "expired"  # collected once
    sent = [m for m, _ in bus.auth.telegram.call.sent]
    assert "sendMessage" in sent and "answerCallbackQuery" in sent


def test_telegram_refusals(tmp_path, monkeypatch):
    bus, c = make(tmp_path, monkeypatch, ON)
    code = telegram_start(bus, c)
    telegram_approve(bus, code, 999)  # not on the allowlist
    r = c.get("/auth/telegram/status").json()
    assert r["status"] == "denied" and "tg:999" in r["reason"]
    code = telegram_start(bus, c)
    telegram_approve(bus, code, 42, verb="no")
    assert c.get("/auth/telegram/status").json()["status"] == "denied"
    code = telegram_start(bus, c)
    tg = bus.auth.telegram
    asyncio.run(tg.handle({"message": {"chat": {"id": 42, "type": "private"}, "from": {"id": 42}, "text": f"/start {code}"}}))
    asyncio.run(tg.handle({"callback_query": {"id": "q", "from": {"id": 77}, "data": f"ok:{code}", "message": {}}}))
    assert c.get("/auth/telegram/status").json()["status"] == "pending"  # only whoever opened the link may approve
    assert c.get("/api/tasks").status_code == 401


# ---- linking ---------------------------------------------------------------------------------------------------
def test_link_telegram_to_google(tmp_path, monkeypatch):
    bus, c = make(tmp_path, monkeypatch, {**ON, "allow": ["me@gmail.com"]})
    google_login(bus, c)
    code = telegram_start(bus, c, link=True)
    telegram_approve(bus, code, 555)  # not on the allowlist itself: linking needs the signed-in person
    assert c.get("/auth/telegram/status").json() == {"status": "approved", "next": "/office", "linked": True}
    me = c.get("/api/auth/me").json()["user"]
    assert [i["id"] for i in me["identities"]] == ["google:me@gmail.com", "tg:555"] and me["can_link"] == []
    links = json.loads((tmp_path / "data" / "auth" / "links.json").read_text())
    assert links["links"] == {"tg:555": "google:me@gmail.com"}

    c2 = TestClient(c.app)  # now Telegram signs in as the same person
    code = telegram_start(bus, c2)
    telegram_approve(bus, code, 555)
    assert c2.get("/auth/telegram/status").json()["status"] == "approved"
    assert c2.get("/api/auth/me").json()["user"]["person"] == "google:me@gmail.com"

    assert c.post("/api/auth/unlink", json={"identity": "google:me@gmail.com"}, headers=ORIGIN).status_code == 400
    assert c.post("/api/auth/unlink", json={"identity": "tg:555"}, headers=ORIGIN).status_code == 200
    assert c2.get("/api/tasks").status_code == 401  # that Telegram session ends with the link


def test_link_google_to_telegram_and_conflicts(tmp_path, monkeypatch):
    bus, c = make(tmp_path, monkeypatch, {**ON, "allow": ["tg:42", "other@gmail.com"]})
    code = telegram_start(bus, c)
    telegram_approve(bus, code, 42)
    c.get("/auth/telegram/status")
    r = google_login(bus, c, email="me@gmail.com", link=True)
    assert r.headers["location"] == "/" and bus.auth.links.person("google:me@gmail.com") == "tg:42"
    c2 = TestClient(c.app)
    google_login(bus, c2, email="other@gmail.com")
    r = google_login(bus, c2, email="me@gmail.com", link=True)  # already linked to someone else
    assert "auth_error" in r.headers["location"]
    assert bus.auth.links.person("google:me@gmail.com") == "tg:42"


def test_linking_can_be_turned_off(tmp_path, monkeypatch):
    bus, c = make(tmp_path, monkeypatch, {**ON, "linking": False})
    google_login(bus, c)
    assert c.get("/api/auth/me").json()["user"]["can_link"] == []
    code = telegram_start(bus, c, link=True)
    telegram_approve(bus, code, 555)
    assert "turned off" in c.get("/auth/telegram/status").json()["reason"]
    assert c.post("/auth/telegram/start", json={"link": True}, headers=ORIGIN).status_code == 200
    assert TestClient(c.app).post("/auth/telegram/start", json={"link": True}, headers=ORIGIN).status_code == 401


# ---- office relay and CLI -----------------------------------------------------------------------------------
def test_office_relay_sends_a_token(monkeypatch):
    from xqo.plugins.office.view import OfficeConfig, relay_headers
    monkeypatch.setenv("HQB_TOKEN", "bbb")
    monkeypatch.setenv("XQO_API_TOKEN", "own")
    cfg = OfficeConfig.from_dict({"hqs": [{"name": "b", "url": "http://127.0.0.1:8002", "token_env": "HQB_TOKEN"},
                                          {"name": "c", "url": "http://127.0.0.1:8003"}]})
    assert relay_headers(cfg, "b") == {"Authorization": "Bearer bbb"}
    assert relay_headers(cfg, "c") == {"Authorization": "Bearer own"}
    with pytest.raises(ValueError, match="token_env"):
        OfficeConfig.from_dict({"hqs": [{"name": "b", "url": "http://x:1", "token_env": "no-pe"}]})


def test_cli_sends_the_token(monkeypatch):
    import urllib.request
    from xqo import client
    seen = {}

    class R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def read(self):
            return b"{}"

    def fake(req, timeout):
        seen.update(req.headers)
        return R()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    monkeypatch.setenv("XQO_API_TOKEN", "t1")
    client.api("http://x", "GET", "/api/tasks")
    assert seen["Authorization"] == "Bearer t1"
