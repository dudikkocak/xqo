"""SMS through Twilio: polling the Messages list (no public URL needed) or a signed webhook."""
from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl

from .base import Adapter, ChannelError, Inbound, Response, secret

API = "https://api.twilio.com/2010-04-01"
EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


def twilio_signature(auth_token: str, url: str, params: dict) -> str:
    """X-Twilio-Signature: base64 HMAC-SHA1 of the full URL followed by the POST parameters sorted by name."""
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    return base64.b64encode(hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()).decode()


class Twilio(Adapter):
    kind = "sms"
    max_len = 1600  # Twilio joins longer texts itself up to this size

    def check(self) -> None:
        self.account = self.cfg.get("account_sid") or ""
        if not self.account:
            raise ChannelError("account_sid is required (Twilio console)")
        self.auth_token = secret(self.cfg, "auth_token")
        self.key_sid = self.cfg.get("api_key_sid")  # optional API key instead of the account's auth token
        self.key_secret = secret(self.cfg, "api_key_secret", required=bool(self.key_sid))
        self.number = self.cfg.get("number") or ""  # your Twilio number, +E.164
        self.service = self.cfg.get("messaging_service_sid")
        if not self.number:
            raise ChannelError("number is required (your Twilio phone number, e.g. +15551234567)")
        self.mode = self.cfg.get("mode", "poll")
        if self.mode not in ("poll", "webhook"):
            raise ChannelError("mode must be poll or webhook")
        self.webhook_url = self.cfg.get("webhook_url")
        if self.mode == "webhook" and not self.webhook_url:
            raise ChannelError("webhook_url is required in webhook mode (the exact public URL Twilio calls; it is signed)")
        self.base = self.cfg.get("api_base", API)
        cur = self.hub.cursor(self.name)
        self.newest = cur  # RFC 2822 date of the newest message seen

    def auth(self) -> tuple[str, str]:
        return (self.key_sid, self.key_secret) if self.key_sid else (self.account, self.auth_token)

    def polls(self) -> bool:
        return self.mode == "poll"

    def poll_interval(self) -> float:
        return max(5.0, float(self.cfg.get("poll_seconds", 20)))

    @staticmethod
    def _when(s: str | None) -> datetime:
        try:
            return parsedate_to_datetime(s) if s else datetime.min.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return datetime.min.replace(tzinfo=timezone.utc)

    async def poll(self) -> None:
        data = await self._json("GET", f"{self.base}/Accounts/{self.account}/Messages.json", auth=self.auth(), timeout=30,
                                params={"To": self.number, "PageSize": int(self.cfg.get("max_results", 50))})
        msgs = [m for m in data.get("messages") or [] if (m.get("direction") or "").startswith("inbound")]
        seen = self.hub.state["seen"].setdefault(self.name, [])
        if self.newest is None:  # first run: only texts that arrive from now on
            seen.extend(m["sid"] for m in msgs if m.get("sid"))
            newest = max((self._when(m.get("date_sent") or m.get("date_created")) for m in msgs), default=None)
            self.newest = newest.strftime("%a, %d %b %Y %H:%M:%S +0000") if newest else datetime.now(timezone.utc).strftime(
                "%a, %d %b %Y %H:%M:%S +0000")
            self.hub.set_cursor(self.name, self.newest)
            return
        floor = self._when(self.newest)
        fresh = sorted((m for m in msgs if self._when(m.get("date_sent") or m.get("date_created")) >= floor),
                       key=lambda m: self._when(m.get("date_sent") or m.get("date_created")))
        for m in fresh:
            if m.get("sid") in seen:  # same second as the cursor
                continue
            await self.hub.receive(Inbound(self.name, m["from"], m["from"], m.get("body") or "", m.get("sid", ""), m["from"],
                                           handles=[m["from"]]))
            self.newest = m.get("date_sent") or m.get("date_created") or self.newest
        if fresh:
            self.hub.set_cursor(self.name, self.newest)

    async def webhook(self, method: str, headers: dict, query: dict, body: bytes) -> Response:
        if method != "POST":
            return Response(405, "POST only")
        params = dict(parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True))
        expected = twilio_signature(self.auth_token, self.webhook_url, params)
        if not hmac.compare_digest(headers.get("x-twilio-signature", ""), expected):
            return Response(403, "bad signature")
        if params.get("AccountSid") and params["AccountSid"] != self.account:
            return Response(403, "wrong account")
        frm = params.get("From", "")
        if frm:
            text = params.get("Body", "")
            if int(params.get("NumMedia", "0") or 0):
                text += f"\n(sent {params['NumMedia']} picture(s), which this channel does not read)"
            await self.hub.receive(Inbound(self.name, frm, frm, text.strip(), params.get("MessageSid", ""), frm,
                                           handles=[frm]))
        return Response(200, EMPTY_TWIML, "text/xml")  # an empty reply: our answer is sent through the API

    async def send(self, chat: str, text: str, options=None, reply_to: str = "") -> str | None:
        form = {"To": chat, "Body": text}
        if self.service:
            form["MessagingServiceSid"] = self.service
        else:
            form["From"] = self.number
        data = await self._json("POST", f"{self.base}/Accounts/{self.account}/Messages.json", auth=self.auth(),
                                data=form, timeout=30)
        return data.get("sid")
