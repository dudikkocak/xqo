"""Shared pieces of the channel adapters: the inbound message, the adapter base class and small helpers."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from . import ChannelHub


class ChannelError(RuntimeError):
    pass


@dataclass
class Inbound:
    """One message from a platform, normalised."""
    channel: str
    chat: str                  # where replies go (chat id, phone number, user id, thread key)
    user: str                  # who wrote it (platform user id)
    text: str = ""
    id: str = ""               # platform message id (for de-duplication and threaded replies)
    name: str = ""             # display name or @username, when the platform gives one
    handles: list[str] = field(default_factory=list)  # other ids the operators list may use (@username, phone)
    action: str = ""           # a pressed button's payload ("a:<attention id>:<option index>")
    files: list[tuple[str, str | None, bytes]] = field(default_factory=list)  # (name, media type, data)
    reply_to: str = ""         # platform id to answer to (Threads: the reply's id)
    group: bool = False        # not a one-to-one chat
    verified: bool = True      # the sender's identity is trustworthy enough to match the operators list (email: DMARC/DKIM)


@dataclass
class Response:
    """What a webhook call answers to the platform."""
    status: int = 200
    body: str | dict = "ok"
    media_type: str = "text/plain"


def secret(cfg: dict, key: str, required: bool = True) -> str | None:
    """Read `<key>_env` (an environment variable name) from a channel config. Secrets never live in the file."""
    env = cfg.get(f"{key}_env")
    value = os.getenv(env) if env else None
    if required and not value:
        raise ChannelError(f"set {key}_env to an environment variable that holds the {key.replace('_', ' ')}"
                           + (f" ({env} is empty)" if env else ""))
    return value


def hmac_sha256(key: str, data: bytes) -> bytes:
    return hmac.new(key.encode(), data, hashlib.sha256).digest()


def meta_signature_ok(app_secret: str, body: bytes, header: str | None) -> bool:
    """Meta webhooks (WhatsApp, Instagram, Threads): X-Hub-Signature-256 = sha256=<hex HMAC of the body>."""
    if not header or not header.startswith("sha256="):
        return False
    return hmac.compare_digest(header[7:], hmac_sha256(app_secret, body).hex())


def b64_hmac(key: str, data: bytes) -> str:
    return "sha256=" + base64.b64encode(hmac_sha256(key, data)).decode()


def chunks(text: str, size: int) -> list[str]:
    """Split a long message at paragraph, line or word breaks so each part fits the platform's limit."""
    text = text.strip()
    out = []
    while len(text) > size:
        cut = max(text.rfind("\n\n", 0, size), text.rfind("\n", 0, size), text.rfind(" ", 0, size))
        cut = cut if cut > size // 2 else size
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        out.append(text)
    return out


class Adapter:
    """One platform. Subclasses fill in parse/send and, where the platform has no webhooks, poll."""
    kind = ""
    max_len = 4096             # characters per message
    window_hours: float | None = None  # free-form replies allowed only this long after the user's last message
    buttons = 0                # answer buttons per message (0: options are listed as text)
    operators_ok = True        # private chats; False for public replies (Threads)
    webhook_only = False

    def __init__(self, hub: ChannelHub, name: str, cfg: dict):
        self.hub, self.name, self.cfg = hub, name, cfg
        self.http: httpx.AsyncClient | None = None
        self._loop: asyncio.Task | None = None
        self.error: str | None = None   # last error, shown in /api/channels
        self.check()

    def check(self) -> None:
        """Validate the config (raise ChannelError); secrets are read here so a missing one fails at startup."""

    def client(self) -> httpx.AsyncClient:
        if self.http is None:
            self.http = self.hub.client_factory()
        return self.http

    async def start(self) -> None:
        if self.polls():
            self._loop = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if self._loop:
            self._loop.cancel()
            try:
                await self._loop
            except (asyncio.CancelledError, Exception):
                pass
            self._loop = None
        if self.http:
            await self.http.aclose()
            self.http = None

    def polls(self) -> bool:
        return False

    async def _poll_loop(self) -> None:
        delay = 1.0
        while True:
            if getattr(self.hub.bus, "on_hold", lambda: False)():  # draining or a backup: messages wait upstream
                await asyncio.sleep(1)
                continue
            try:
                await self.poll()
                self.error, delay = None, 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # network trouble: back off, never give up
                self.error = f"{type(e).__name__}: {e}"
                self.hub.log_error(self.name, f"poll failed: {self.error}")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)
            await asyncio.sleep(self.poll_interval())

    def poll_interval(self) -> float:
        return float(self.cfg.get("poll_seconds", 0))

    async def poll(self) -> None:
        raise NotImplementedError

    async def webhook(self, method: str, headers: dict, query: dict, body: bytes) -> Response:
        return Response(404, "this channel has no webhook")

    async def send(self, chat: str, text: str, options: list[tuple[str, str]] | None = None,
                   reply_to: str = "") -> str | None:
        """Send one message (already cut to max_len). options: [(label, payload)] answer buttons."""
        raise NotImplementedError

    def status(self) -> dict:
        return {"type": self.kind, "error": self.error, "polling": self.polls(), "max_len": self.max_len,
                "window_hours": self.window_hours}

    async def _json(self, method: str, url: str, **kw) -> dict:
        r = await self.client().request(method, url, **kw)
        try:
            data = r.json()
        except ValueError:
            data = {"raw": r.text[:500]}
        if r.status_code >= 400:
            raise ChannelError(f"{self.name}: HTTP {r.status_code}: {str(data)[:300]}")
        return data
