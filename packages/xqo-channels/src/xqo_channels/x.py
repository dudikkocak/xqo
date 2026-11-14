"""X (Twitter) direct messages: polling GET /2/dm_events (no public URL needed) or Account Activity webhooks.

X bills per DM read and sent, so polling is slow by default (poll_seconds, 60) and the first poll only marks
the existing messages as seen."""
from __future__ import annotations

import hmac
import json
import time

from .base import Adapter, ChannelError, Inbound, Response, b64_hmac, secret

API = "https://api.x.com/2"
FIELDS = "id,text,event_type,sender_id,dm_conversation_id,created_at"


class X(Adapter):
    kind = "x"
    max_len = 10000

    def check(self) -> None:
        self.mode = self.cfg.get("mode", "poll")
        if self.mode not in ("poll", "webhook"):
            raise ChannelError("x.mode must be poll or webhook")
        tokens = self.hub.load_secret_file(self.name)  # refreshed tokens, when a refresh token is configured
        self.token = tokens.get("access_token") or secret(self.cfg, "token")
        self.refresh_token = tokens.get("refresh_token") or secret(self.cfg, "refresh_token", required=False)
        self.client_id = self.cfg.get("client_id")
        self.client_secret = secret(self.cfg, "client_secret", required=False)
        self.consumer_secret = secret(self.cfg, "consumer_secret", required=self.mode == "webhook")
        self.user_id = str(self.cfg.get("user_id") or "")
        self.base = self.cfg.get("api_base", API)
        cur = self.hub.cursor(self.name)
        self.newest = int(cur) if cur else None

    def polls(self) -> bool:
        return self.mode == "poll"

    def poll_interval(self) -> float:
        return max(15.0, float(self.cfg.get("poll_seconds", 60)))

    async def api(self, method: str, path: str, **kw) -> dict:
        for attempt in (1, 2):
            r = await self.client().request(method, f"{self.base}{path}", timeout=30,
                                            headers={"Authorization": f"Bearer {self.token}"}, **kw)
            if r.status_code == 401 and attempt == 1 and await self.refresh():
                continue
            try:
                data = r.json()
            except ValueError:
                data = {"raw": r.text[:300]}
            if r.status_code >= 400:
                raise ChannelError(f"x: HTTP {r.status_code}: {str(data)[:300]}")
            return data
        return {}

    async def refresh(self) -> bool:
        """OAuth 2.0 refresh (offline.access scope); the new tokens are kept in the data dir (mode 600)."""
        if not (self.refresh_token and self.client_id):
            return False
        auth = (self.client_id, self.client_secret) if self.client_secret else None
        r = await self.client().post(f"{self.base}/oauth2/token", auth=auth, timeout=30, data={
            "grant_type": "refresh_token", "refresh_token": self.refresh_token, "client_id": self.client_id})
        if r.status_code >= 400:
            self.hub.log_error(self.name, f"token refresh failed: HTTP {r.status_code}")
            return False
        d = r.json()
        self.token, self.refresh_token = d["access_token"], d.get("refresh_token", self.refresh_token)
        self.hub.save_secret_file(self.name, {"access_token": self.token, "refresh_token": self.refresh_token,
                                              "saved": time.time()})
        return True

    async def me(self) -> str:
        if not self.user_id:
            self.user_id = str(((await self.api("GET", "/users/me")).get("data") or {}).get("id", ""))
        return self.user_id

    async def poll(self) -> None:
        own = await self.me()
        data = await self.api("GET", "/dm_events", params={"dm_event.fields": FIELDS, "event_types": "MessageCreate",
                                                           "max_results": int(self.cfg.get("max_results", 50))})
        events = [e for e in data.get("data") or [] if e.get("event_type") == "MessageCreate"]
        ids = [int(e["id"]) for e in events if str(e.get("id", "")).isdigit()]
        if self.newest is None:  # first run: answer only what arrives from now on
            self.newest = max(ids, default=0)
            self.hub.set_cursor(self.name, str(self.newest))
            return
        for e in sorted(events, key=lambda e: int(e["id"])):
            if int(e["id"]) <= self.newest:
                continue
            self.newest = int(e["id"])
            sender = str(e.get("sender_id", ""))
            if sender and sender != own:
                await self.hub.receive(Inbound(self.name, sender, sender, e.get("text", ""), e["id"], sender))
        self.hub.set_cursor(self.name, str(self.newest))

    async def webhook(self, method: str, headers: dict, query: dict, body: bytes) -> Response:
        if method == "GET":  # CRC check: X proves we hold the consumer secret
            token = query.get("crc_token")
            if not token:
                return Response(400, "missing crc_token")
            return Response(200, {"response_token": b64_hmac(self.consumer_secret, token.encode())})
        sig = headers.get("x-twitter-webhooks-signature", "")
        if not hmac.compare_digest(sig, b64_hmac(self.consumer_secret, body)):
            return Response(403, "bad signature")
        payload = json.loads(body or b"{}")
        own = self.user_id or str(payload.get("for_user_id", ""))
        users = payload.get("users") or {}
        for e in payload.get("direct_message_events") or []:
            mc = e.get("message_create") or {}
            sender = str(mc.get("sender_id", ""))
            if e.get("type") != "message_create" or not sender or sender == own:
                continue
            u = users.get(sender) or {}
            name = f"@{u['screen_name']}" if u.get("screen_name") else sender
            await self.hub.receive(Inbound(self.name, sender, sender, (mc.get("message_data") or {}).get("text", ""),
                                           str(e.get("id", "")), name, handles=[name] if name != sender else []))
        return Response(200, "ok")

    async def send(self, chat: str, text: str, options=None, reply_to: str = "") -> str | None:
        data = await self.api("POST", f"/dm_conversations/with/{chat}/messages", json={"text": text})
        return (data.get("data") or {}).get("dm_event_id")
