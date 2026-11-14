"""Gmail: polls the inbox through the Gmail API and answers in the same thread.

OAuth 2.0 with a refresh token (scopes gmail.readonly + gmail.send, plus gmail.modify for "mark_read").
Email senders are easy to forge, so an operator's mail counts only when Gmail saw it pass DMARC or DKIM."""
from __future__ import annotations

import base64
import html
import re
import time
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

from .base import Adapter, ChannelError, Inbound, secret

API = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_URL = "https://oauth2.googleapis.com/token"
QUOTE = re.compile(r"^(On .{5,200} wrote:|-----Original Message-----|From: .+|Le .{5,200} a écrit :|Am .{5,200} schrieb .+:)\s*$",
                   re.M)


def _b64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def body_text(payload: dict) -> str:
    """The plain-text body (or the HTML one with tags removed), without the quoted earlier mails."""
    plain, rich = [], []

    def walk(p):
        mt, data = p.get("mimeType", ""), (p.get("body") or {}).get("data")
        if data and mt == "text/plain":
            plain.append(_b64(data).decode("utf-8", "replace"))
        elif data and mt == "text/html":
            rich.append(_b64(data).decode("utf-8", "replace"))
        for sub in p.get("parts") or []:
            walk(sub)
    walk(payload)
    text = "\n".join(plain)
    if not text and rich:
        text = html.unescape(re.sub(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", " ", "\n".join(rich), flags=re.S | re.I))
    m = QUOTE.search(text)
    if m:
        text = text[: m.start()]
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith(">")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


class Gmail(Adapter):
    kind = "gmail"
    max_len = 50000
    buttons = 0

    def check(self) -> None:
        self.client_id = self.cfg.get("client_id")
        if not self.client_id:
            raise ChannelError("gmail.client_id is required (Google Cloud console → OAuth client)")
        self.client_secret = secret(self.cfg, "client_secret")
        saved = self.hub.load_secret_file(self.name)
        self.refresh_token = saved.get("refresh_token") or secret(self.cfg, "refresh_token")
        self.address = (self.cfg.get("address") or "").lower()  # looked up when left out
        self.query = self.cfg.get("query", "in:inbox -category:promotions -category:social")
        self.base = self.cfg.get("api_base", API)
        self.token, self.expires = "", 0.0
        st = self.hub.state.setdefault("mail", {}).setdefault(self.name, {})
        self.threads: dict = st.setdefault("threads", {})  # sender -> {thread, subject, msgid, refs}
        self.since = st.get("since")

    def polls(self) -> bool:
        return True

    def poll_interval(self) -> float:
        return max(15.0, float(self.cfg.get("poll_seconds", 60)))

    async def access_token(self) -> str:
        if self.token and time.time() < self.expires - 60:
            return self.token
        r = await self.client().post(self.cfg.get("token_url", TOKEN_URL), timeout=30, data={
            "grant_type": "refresh_token", "refresh_token": self.refresh_token,
            "client_id": self.client_id, "client_secret": self.client_secret})
        if r.status_code >= 400:
            raise ChannelError(f"gmail: token refresh failed: HTTP {r.status_code} {r.text[:200]}")
        d = r.json()
        self.token, self.expires = d["access_token"], time.time() + float(d.get("expires_in", 3600))
        if d.get("refresh_token"):  # Google rotated it
            self.refresh_token = d["refresh_token"]
            self.hub.save_secret_file(self.name, {"refresh_token": self.refresh_token})
        return self.token

    async def api(self, method: str, path: str, **kw) -> dict:
        return await self._json(method, f"{self.base}{path}", timeout=30,
                                headers={"Authorization": f"Bearer {await self.access_token()}"}, **kw)

    async def poll(self) -> None:
        if not self.address:
            self.address = (await self.api("GET", "/profile")).get("emailAddress", "").lower()
        st = self.hub.state["mail"][self.name]
        if self.since is None:  # first run: only mail that arrives from now on
            self.since = st["since"] = int(time.time())
            self.hub.save()
            return
        q = f"{self.query} after:{int(self.since) - 60}"
        listing = await self.api("GET", "/messages", params={"q": q, "maxResults": int(self.cfg.get("max_results", 25))})
        for ref in reversed(listing.get("messages") or []):
            if ref["id"] in (self.hub.state["seen"].get(self.name) or []):
                continue
            m = await self.api("GET", f"/messages/{ref['id']}", params={"format": "full"})
            msg = self.parse(m)
            if msg:
                await self.hub.receive(msg)
            else:
                self.hub.state["seen"].setdefault(self.name, []).append(ref["id"])
            if self.cfg.get("mark_read") and msg:
                try:
                    await self.api("POST", f"/messages/{ref['id']}/modify", json={"removeLabelIds": ["UNREAD"]})
                except Exception as e:
                    self.hub.log_error(self.name, f"could not mark {ref['id']} read: {e}")
        st["since"] = self.since = max(int(self.since), int(time.time()) - 3600)
        self.hub.save()

    def parse(self, m: dict) -> Inbound | None:
        """A mail worth answering, or None (our own, automatic, bulk or list mail)."""
        payload = m.get("payload") or {}
        h = {x["name"].lower(): x["value"] for x in payload.get("headers") or []}
        name, addr = parseaddr(h.get("reply-to") or h.get("from", ""))
        _, frm = parseaddr(h.get("from", ""))
        addr, frm = addr.lower(), frm.lower()
        auto = h.get("auto-submitted", "no").lower() != "no" or h.get("precedence", "").lower() in ("bulk", "list", "junk") \
            or "list-id" in h or "list-unsubscribe" in h or frm.startswith(("mailer-daemon@", "noreply@", "no-reply@"))
        if not addr or frm == self.address or auto or "SENT" in (m.get("labelIds") or []):
            return None
        auth = h.get("authentication-results", "").lower()
        verified = "dmarc=pass" in auth or "dkim=pass" in auth
        subject = h.get("subject", "")
        refs = (h.get("references", "") + " " + h.get("message-id", "")).strip()
        self.threads[addr] = {"thread": m.get("threadId"), "subject": subject, "msgid": h.get("message-id", ""),
                              "refs": refs[-2000:]}
        text = body_text(payload)
        if subject and not re.match(r"^\s*(re|aw|sv|fw|fwd)\s*:", subject, re.I):
            if subject.startswith("/"):  # a command in the subject line: "/status", "/new write the report"
                text = subject + (f"\n\n{text}" if text else "")
            elif text.startswith("/"):
                text += f"\n\n(subject: {subject})"
            else:
                text = f"Subject: {subject}\n\n{text}"
        return Inbound(self.name, addr, addr, text, m["id"], formataddr((name, addr)) if name else addr,
                       verified=verified or not self.cfg.get("verify_operators", True))

    async def send(self, chat: str, text: str, options=None, reply_to: str = "") -> str | None:
        t = self.threads.get(chat) or {}
        mail = EmailMessage()
        mail["To"] = chat
        if self.address:
            mail["From"] = formataddr((self.cfg.get("display_name", ""), self.address)) if self.cfg.get("display_name") else self.address
        subject = t.get("subject") or self.cfg.get("subject", "Message from your HQ")
        mail["Subject"] = subject if re.match(r"^\s*re\s*:", subject, re.I) or not t else f"Re: {subject}"
        if t.get("msgid"):
            mail["In-Reply-To"] = t["msgid"]
            mail["References"] = t.get("refs") or t["msgid"]
        mail["Auto-Submitted"] = "auto-replied"  # keeps other robots from answering us
        mail.set_content(text)
        body = {"raw": base64.urlsafe_b64encode(mail.as_bytes()).decode()}
        if t.get("thread"):
            body["threadId"] = t["thread"]
        return (await self.api("POST", "/messages/send", json=body)).get("id")
