"""Telegram Bot API: long polling (no public URL needed) or a webhook."""
from __future__ import annotations

import hmac
import json

from .base import Adapter, ChannelError, Inbound, Response, secret

API = "https://api.telegram.org"


class Telegram(Adapter):
    kind = "telegram"
    max_len = 4096
    buttons = 8

    def check(self) -> None:
        self.token = secret(self.cfg, "token")
        self.mode = self.cfg.get("mode", "poll")
        if self.mode not in ("poll", "webhook"):
            raise ChannelError("telegram.mode must be poll or webhook")
        self.webhook_secret = secret(self.cfg, "webhook_secret", required=self.mode == "webhook")
        self.offset = int(self.hub.cursor(self.name) or 0)

    def url(self, method: str) -> str:
        return f"{API}/bot{self.token}/{method}"

    async def call(self, method: str, **params) -> dict:
        data = await self._json("POST", self.url(method), json=params, timeout=params.get("timeout", 0) + 20)
        if not data.get("ok"):
            raise ChannelError(f"telegram {method}: {data.get('description', data)}")
        return data["result"]

    async def start(self) -> None:
        try:
            if self.mode == "webhook" and self.cfg.get("webhook_url"):
                await self.call("setWebhook", url=self.cfg["webhook_url"], secret_token=self.webhook_secret,
                                allowed_updates=["message", "callback_query"])
            elif self.mode == "poll":
                await self.call("deleteWebhook")  # getUpdates refuses to work while a webhook is set
        except Exception as e:  # no network yet: polling retries with back-off
            self.error = f"{type(e).__name__}: {e}"
            self.hub.log_error(self.name, f"setup failed: {self.error}")
        await super().start()

    def polls(self) -> bool:
        return self.mode == "poll"

    async def poll(self) -> None:
        updates = await self.call("getUpdates", offset=self.offset, timeout=int(self.cfg.get("poll_timeout", 25)),
                                  allowed_updates=["message", "callback_query"])
        for u in updates:
            self.offset = max(self.offset, u["update_id"] + 1)
            await self.handle(u)
        if updates:
            self.hub.set_cursor(self.name, self.offset)

    async def webhook(self, method: str, headers: dict, query: dict, body: bytes) -> Response:
        got = headers.get("x-telegram-bot-api-secret-token", "")
        if method != "POST" or not self.webhook_secret or not hmac.compare_digest(got, self.webhook_secret):
            return Response(403, "forbidden")
        await self.handle(json.loads(body or b"{}"))
        return Response(200, "ok")

    async def handle(self, u: dict) -> None:
        if "callback_query" in u:
            q = u["callback_query"]
            frm, msg = q.get("from") or {}, q.get("message") or {}
            try:
                await self.call("answerCallbackQuery", callback_query_id=q["id"])
            except Exception:
                pass
            await self.hub.receive(self.inbound(frm, msg.get("chat") or {}, f"cb{q['id']}", action=q.get("data", "")))
            return
        msg = u.get("message")
        if not msg or "from" not in msg:
            return
        files = await self.files(msg)
        await self.hub.receive(self.inbound(msg["from"], msg["chat"], str(msg["message_id"]),
                                            text=msg.get("text") or msg.get("caption") or "", files=files))

    def inbound(self, frm: dict, chat: dict, mid: str, text: str = "", action: str = "",
                files: list | None = None) -> Inbound:
        user = str(frm.get("id", ""))
        uname = frm.get("username")
        name = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x) or (f"@{uname}" if uname else user)
        return Inbound(self.name, str(chat.get("id", user)), user, text, mid, name,
                       handles=[f"@{uname}"] if uname else [], action=action, files=files or [],
                       group=chat.get("type", "private") != "private")

    async def files(self, msg: dict) -> list:
        """Photos (largest size) and documents, downloaded through getFile."""
        items = []
        if msg.get("photo"):
            items.append((msg["photo"][-1]["file_id"], f"photo_{msg['message_id']}.jpg", "image/jpeg"))
        if msg.get("document"):
            d = msg["document"]
            items.append((d["file_id"], d.get("file_name") or f"file_{msg['message_id']}", d.get("mime_type")))
        out = []
        for fid, name, mt in items:
            try:
                info = await self.call("getFile", file_id=fid)
                r = await self.client().get(f"{API}/file/bot{self.token}/{info['file_path']}", timeout=60)
                r.raise_for_status()
                out.append((name, mt, r.content))
            except Exception as e:
                self.hub.log_error(self.name, f"could not download {name}: {e}")
        return out

    async def send(self, chat: str, text: str, options=None, reply_to: str = "") -> str | None:
        params: dict = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
        if options:
            params["reply_markup"] = {"inline_keyboard": [[{"text": label[:60], "callback_data": payload[:64]}]
                                                          for label, payload in options]}
        if reply_to and reply_to.isdigit():
            params["reply_parameters"] = {"message_id": int(reply_to), "allow_sending_without_reply": True}
        res = await self.call("sendMessage", **params)
        return str(res.get("message_id", ""))
