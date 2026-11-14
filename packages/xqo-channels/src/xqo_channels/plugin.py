"""Channels plugin: messaging channels (Telegram, WhatsApp, Instagram, X, Threads, Gmail, SMS) as a front door.

On when "channels" is in agents.json (see CHANNELS.md). The hub owns the chat <-> task mapping, the access modes
(none / operator / responder) and the responder tool filter, so an adapter can only deliver and receive messages,
never widen what a public-facing task may do. Adapters are a registry: built-ins plus installed packages with an
entry point in the group ``xqo.channels`` (``discord = "pkg.module:DiscordAdapter"``, a channels.base.Adapter)."""
from __future__ import annotations

import sys

from fastapi import HTTPException, Request

from xqo.api import client_api as _api
from xqo.api import Plugin
from . import ADAPTERS, ChannelHub, parse_access
from .base import Adapter

ENTRY_POINT_GROUP = "xqo.channels"


def register_adapter(kind: str, cls: type[Adapter]) -> type[Adapter]:
    """Make an adapter class usable as "type": kind in a channel's configuration."""
    if not (isinstance(cls, type) and issubclass(cls, Adapter)):
        raise TypeError(f"{cls!r} is not a channels.base.Adapter subclass")
    ADAPTERS[kind] = cls
    return cls


def _channels(a) -> None:
    """channels: the messaging channels of the running server, which agents serve them, and recent traffic."""
    try:
        st = _api(a.url, "GET", "/api/channels")
    except RuntimeError as e:
        sys.exit(str(e))
    except ConnectionError:
        sys.exit(f"no server answering at {a.url}")
    if not st.get("enabled"):
        print("no channels configured (\"channels\" in agents.json)")
        return
    for name, c in st["channels"].items():
        print(f"  {name:<12} {c['type']:<9} operators={c['operators']:<3} operator agent={c['operator_agent'] or '-':<12} "
              f"responder agent={c['responder_agent'] or '-':<12} approve={'yes' if c['approve_replies'] else 'no':<3} "
              f"conversations={c['conversations']} sent today={c['sent_today']}" + (f"  ERROR: {c['error']}" if c["error"] else ""))
    for w in st.get("warnings") or []:
        print(f"warning: {w}")
    for m in st.get("recent", [])[-a.limit:]:
        print(f"{m['ts'][:19]}  {m['dir']:<7} {m['channel']}:{m['chat']:<16} {(m.get('text') or '')[:70]!r}")


class ChannelsPlugin(Plugin):
    name = "channels"
    description = "Messaging channels: Telegram, WhatsApp, Instagram, X, Threads, Gmail, SMS"
    service = "channels"
    config_keys = ("channels",)
    state_dirs = ("channels",)
    agent_keys = ("channels", "channel_tools")
    tool_names = frozenset({"channel_send", "channel_inbox"})

    @classmethod
    def register(cls) -> None:
        try:
            from importlib.metadata import entry_points
            eps = list(entry_points(group=ENTRY_POINT_GROUP))
        except Exception:
            eps = []
        for ep in eps:
            try:
                register_adapter(ep.name, ep.load())
            except Exception as e:  # one broken adapter package must not take the others down
                print(f"warning: channel adapter {ep.name} ({ep.value}) could not be loaded: {e}", file=sys.stderr)

    @classmethod
    def add_cli(cls, sub, url) -> list[str]:
        ch = sub.add_parser("channels", help="messaging channels: status, agents and recent traffic")
        ch.add_argument("--url", default=url, help="server address (XQO_URL)")
        ch.add_argument("--limit", type=int, default=20, help="recent channel messages to show")
        return ["channels"]

    @classmethod
    def run_cli(cls, a) -> None:
        _channels(a)

    def agent_config(self, agent, a: dict, dept_cfg) -> None:
        agent.channels = parse_access(a.get("channels"), f"agent {a['name']!r}")  # none|operator|responder
        agent.channel_tools = list(a.get("channel_tools") or [])  # extra tools in tasks that answer the public

    def configure(self, ctx, cfg):
        return ChannelHub.from_config(ctx.bus, cfg.get("channels"), ctx.data_root)

    def check_agents(self) -> list[str]:
        return self.obj.check_agents()

    async def start(self) -> None:
        await self.obj.start()

    async def stop(self) -> None:
        await self.obj.stop()

    def status(self) -> dict:
        st = self.obj.status()
        return {"channels": sorted(st.get("channels", {})), "warnings": st.get("warnings", [])}

    def banner(self) -> list[str]:
        port = self.obj.webhook_cfg.get("port")
        return [f"Channels: {', '.join(self.obj.adapters) or 'none enabled'}"
                + (f"  (webhooks on port {port})" if port else "")]

    def tool_defs(self, agent) -> list[dict]:
        return self.obj.tool_defs(agent)

    async def call_tool(self, task, agent, name: str, inp: dict) -> str:
        return await self.obj.tool(task, agent, name, inp)

    def allows(self, task, agent, tool: str) -> bool:
        return self.obj.allows(task, agent, tool)

    def deny_message(self, tool: str) -> str:
        return f"{tool} is not available while answering a message from the public"

    def filter_tools(self, task, agent, tools, servers):
        return self.obj.filter_tools(task, agent, tools, servers)

    def routes(self, app, bus) -> None:
        # ---- messaging channels (channels/) ----------------------------------------
        @app.get("/api/channels")
        async def channels_status():
            ch = bus.plugins.service("channels")
            return {"enabled": bool(ch), **(ch.status() if ch else {})}

        @app.api_route("/api/channels/{name}/webhook", methods=["GET", "POST"])
        async def channel_webhook(name: str, request: Request):
            """Platform callbacks. Expose only this path to the internet, or use "channels.webhook.port"."""
            ch = bus.plugins.service("channels")
            if not ch:
                raise HTTPException(404, "no channels configured")
            return await ch.http_webhook(name, request)
