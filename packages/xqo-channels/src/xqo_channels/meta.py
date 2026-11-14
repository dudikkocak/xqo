"""Meta platforms: WhatsApp Cloud API, Instagram messaging (Instagram Login) and Threads replies.

All three use webhooks only: Meta calls GET once to verify the URL (hub.verify_token / hub.challenge) and
POSTs events signed with the app secret (X-Hub-Signature-256)."""
from __future__ import annotations

import asyncio
import json

from .base import Adapter, ChannelError, Inbound, Response, meta_signature_ok, secret

GRAPH = "https://graph.facebook.com/v21.0"
IG_GRAPH = "https://graph.instagram.com/v21.0"
THREADS_GRAPH = "https://graph.threads.net/v1.0"


class MetaAdapter(Adapter):
    webhook_only = True

    def check(self) -> None:
        self.token = secret(self.cfg, "token")
        self.app_secret = secret(self.cfg, "app_secret")
        self.verify_token = secret(self.cfg, "verify_token")

    async def webhook(self, method: str, headers: dict, query: dict, body: bytes) -> Response:
        if method == "GET":  # subscription check when the callback URL is saved in the Meta app
            if query.get("hub.mode") == "subscribe" and query.get("hub.verify_token") == self.verify_token:
                return Response(200, query.get("hub.challenge", ""))
            return Response(403, "forbidden")
        if not meta_signature_ok(self.app_secret, body, headers.get("x-hub-signature-256")):
            return Response(403, "bad signature")
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            return Response(400, "bad json")
        for msg in self.parse(payload):
            await self.hub.receive(msg)
        return Response(200, "ok")  # answer fast; the work runs in the background

    def parse(self, payload: dict) -> list[Inbound]:
        raise NotImplementedError

    def auth(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}


class WhatsApp(MetaAdapter):
    kind = "whatsapp"
    max_len = 4096
    window_hours = 24
    buttons = 3

    def check(self) -> None:
        super().check()
        self.phone_id = str(self.cfg.get("phone_number_id") or "")
        if not self.phone_id:
            raise ChannelError("whatsapp.phone_number_id is required (WhatsApp Manager → API setup)")
        self.base = self.cfg.get("api_base", GRAPH)

    def parse(self, payload: dict) -> list[Inbound]:
        out = []
        for entry in payload.get("entry") or []:
            for ch in entry.get("changes") or []:
                v = ch.get("value") or {}
                if ch.get("field") != "messages" or str((v.get("metadata") or {}).get("phone_number_id", self.phone_id)) != self.phone_id:
                    continue
                names = {c.get("wa_id"): (c.get("profile") or {}).get("name", "") for c in v.get("contacts") or []}
                for m in v.get("messages") or []:
                    frm = str(m.get("from", ""))
                    text, action, media = "", "", None
                    t = m.get("type")
                    if t == "text":
                        text = (m.get("text") or {}).get("body", "")
                    elif t == "interactive":
                        r = (m.get("interactive") or {})
                        r = r.get("button_reply") or r.get("list_reply") or {}
                        action, text = r.get("id", ""), r.get("title", "")
                    elif t == "button":
                        action, text = (m.get("button") or {}).get("payload", ""), (m.get("button") or {}).get("text", "")
                    elif t in ("image", "document"):
                        media = m.get(t) or {}
                        text = media.get("caption", "")
                    else:
                        text = f"(a {t} message, which this channel cannot read)"
                    msg = Inbound(self.name, frm, frm, text, m.get("id", ""), names.get(frm) or f"+{frm}",
                                  handles=[f"+{frm}"], action=action)
                    if media:
                        msg.files = [("pending", media.get("mime_type"), json.dumps(
                            {"id": media.get("id"), "name": media.get("filename") or f"{t}_{m.get('id', '')[-8:]}"}).encode())]
                    out.append(msg)
        return out

    async def fetch(self, msg: Inbound) -> None:
        """Download media named in parse() (the webhook only carries its id)."""
        done = []
        for name, mt, data in msg.files:
            if name != "pending":
                done.append((name, mt, data))
                continue
            ref = json.loads(data)
            try:
                info = await self._json("GET", f"{self.base}/{ref['id']}", headers=self.auth(), timeout=30)
                r = await self.client().get(info["url"], headers=self.auth(), timeout=60)
                r.raise_for_status()
                ext = {"image/jpeg": ".jpg", "image/png": ".png", "application/pdf": ".pdf"}.get(info.get("mime_type") or mt, "")
                done.append((ref["name"] + ("" if "." in ref["name"] else ext), info.get("mime_type") or mt, r.content))
            except Exception as e:
                self.hub.log_error(self.name, f"could not download media {ref.get('id')}: {e}")
        msg.files = done

    async def send(self, chat: str, text: str, options=None, reply_to: str = "") -> str | None:
        body: dict = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": chat}
        if options:
            body |= {"type": "interactive", "interactive": {
                "type": "button", "body": {"text": text[:1024]},
                "action": {"buttons": [{"type": "reply", "reply": {"id": p[:256], "title": label[:20]}}
                                       for label, p in options[:3]]}}}
        else:
            body |= {"type": "text", "text": {"body": text, "preview_url": False}}
        if reply_to:
            body["context"] = {"message_id": reply_to}
        data = await self._json("POST", f"{self.base}/{self.phone_id}/messages", headers=self.auth(), json=body, timeout=30)
        return ((data.get("messages") or [{}])[0]).get("id")

    async def send_template(self, chat: str) -> str | None:
        """An approved template, sent when a reply is held outside the 24h window ("template" in the config)."""
        t = self.cfg.get("template")
        if not t:
            return None
        body = {"messaging_product": "whatsapp", "to": chat, "type": "template",
                "template": {"name": t["name"], "language": {"code": t.get("language", "en")}}}
        data = await self._json("POST", f"{self.base}/{self.phone_id}/messages", headers=self.auth(), json=body, timeout=30)
        return ((data.get("messages") or [{}])[0]).get("id")


class Instagram(MetaAdapter):
    kind = "instagram"
    max_len = 1000
    window_hours = 24
    buttons = 13

    def check(self) -> None:
        super().check()
        self.account = str(self.cfg.get("account_id") or "")  # the Instagram professional account's id
        self.base = self.cfg.get("api_base", IG_GRAPH)

    def parse(self, payload: dict) -> list[Inbound]:
        out = []
        if payload.get("object") not in (None, "instagram"):
            return out
        for entry in payload.get("entry") or []:
            own = str(entry.get("id", self.account))
            for ev in entry.get("messaging") or []:
                sender = str((ev.get("sender") or {}).get("id", ""))
                m = ev.get("message") or {}
                if not sender or sender in (own, self.account) or m.get("is_echo"):
                    continue  # our own messages come back as echoes
                pb = ev.get("postback") or {}
                action = (m.get("quick_reply") or {}).get("payload") or pb.get("payload", "")
                text = m.get("text") or pb.get("title", "")
                if not text and m.get("attachments"):
                    text = "(sent " + ", ".join(a.get("type", "a file") for a in m["attachments"]) + ")"
                if not text and not action:
                    continue  # reactions, reads, deletes
                out.append(Inbound(self.name, sender, sender, text, m.get("mid") or pb.get("mid", ""), sender,
                                   action=action))
        return out

    async def send(self, chat: str, text: str, options=None, reply_to: str = "") -> str | None:
        message: dict = {"text": text}
        if options:
            message["quick_replies"] = [{"content_type": "text", "title": label[:20], "payload": p[:1000]}
                                        for label, p in options[:13]]
        data = await self._json("POST", f"{self.base}/{self.account or 'me'}/messages", headers=self.auth(),
                                json={"recipient": {"id": chat}, "message": message}, timeout=30)
        return data.get("message_id")


class Threads(MetaAdapter):
    """Public replies and mentions on the account's Threads posts. Answers are public replies, so the channel
    is for responders only and replies need approval unless approve_replies is false."""
    kind = "threads"
    max_len = 500
    operators_ok = False

    def check(self) -> None:
        super().check()
        self.username = (self.cfg.get("username") or "").lstrip("@").lower()
        if not self.username:
            raise ChannelError("threads.username is required (the account's own posts are ignored)")
        self.base = self.cfg.get("api_base", THREADS_GRAPH)
        self.publish_delay = float(self.cfg.get("publish_delay", 2))

    def parse(self, payload: dict) -> list[Inbound]:
        values = payload.get("values")
        values = values if isinstance(values, list) else [values] if values else []
        out = []
        for item in values:
            if (item or {}).get("field") not in ("replies", "mentions"):
                continue
            v = item.get("value") or {}
            user = (v.get("username") or "").lower()
            if not v.get("id") or not user or user == self.username:
                continue
            root = (v.get("root_post") or {}).get("id") or v["id"]
            # one conversation per person and post; the answer replies to their latest post
            out.append(Inbound(self.name, f"{user}@{root}", user, v.get("text") or "", v["id"], f"@{user}",
                               handles=[f"@{user}"], reply_to=v["id"], group=True))
        return out

    async def send(self, chat: str, text: str, options=None, reply_to: str = "") -> str | None:
        if not reply_to:
            raise ChannelError("threads: nothing to reply to")
        c = await self._json("POST", f"{self.base}/me/threads", headers=self.auth(), timeout=30,
                             params={"media_type": "TEXT", "text": text, "reply_to_id": reply_to})
        await asyncio.sleep(self.publish_delay)  # Meta asks for a short wait before publishing a container
        p = await self._json("POST", f"{self.base}/me/threads_publish", headers=self.auth(), timeout=30,
                             params={"creation_id": c["id"]})
        return p.get("id")
