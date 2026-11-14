"""Feeds plugin: local feeds (system, processes, services, files, logs, journald, docker, push) and rules that
wake agents. Requires the schedules plugin (rule tasks run through it). On when "feeds" is in agents.json (FEEDS.md)."""
from __future__ import annotations

from pathlib import Path

import json
import os
import sys
import urllib.error
import urllib.request

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from xqo.api import client_api as _api
from xqo.api import Plugin
from . import FeedHub, parse_access


def _feed(a) -> None:
    """feed status | feed emit NAME: the local feeds of the running server, or send it an event (cron, git hooks, CI)."""
    if a.feed_cmd == "emit":
        fields = {}
        for kv in a.field or []:
            k, sep, v = kv.partition("=")
            if not sep:
                sys.exit(f"--field {kv!r}: use name=value")
            try:
                fields[k] = json.loads(v)  # numbers, true/false stay typed
            except ValueError:
                fields[k] = v
        if a.status is not None:
            fields["status"] = a.status
        text = sys.stdin.read() if a.text == "-" else (a.text or "")
        sev = a.severity or ("error" if a.status not in (None, 0) else "info")
        body = {"kind": a.kind, "severity": sev, "key": a.key or "", "fields": fields, "text": text[-2000:]}
        token = os.getenv(a.token_env or "") if a.token_env else os.getenv("XQO_FEED_TOKEN")
        req = urllib.request.Request(a.url.rstrip("/") + f"/api/feeds/{a.name}/events", method="POST",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json",
                                              **({"Authorization": f"Bearer {token}"} if token else {})})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                print(f"sent to {a.name}: {json.loads(r.read()).get('accepted', 0)} event(s)")
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read()).get("error") or e.reason
            except ValueError:
                detail = e.reason
            sys.exit(f"{e.code}: {detail}")
        except (urllib.error.URLError, OSError):
            sys.exit(f"no server answering at {a.url}")
        return
    try:
        st = _api(a.url, "GET", "/api/feeds")
    except RuntimeError as e:
        sys.exit(str(e))
    except ConnectionError:
        sys.exit(f"no server answering at {a.url}")
    if not st.get("enabled"):
        print("no feeds configured (\"feeds\" in agents.json)")
        return
    for name, f in st["feeds"].items():
        now = next(iter(f.get("latest", {}).values()), {}).get("fields", {})
        vals = ", ".join(f"{k}={v}" for k, v in list(now.items())[:5])
        print(f"  {name:<14} {f['type']:<9} events={f.get('events', 0):<6} {vals or f['about']}"
              + (f"  ERROR: {f['error']}" if f["error"] else ""))
    for r in st["rules"]:
        c = r["counts"]
        print(f"  rule {r['name']:<18} {r['action']:<7} {r['mode'] if r['action'] in ('wake', 'digest') else '':<7} "
              f"fired={c.get('fired', 0)} woken={c.get('woken', 0)} suppressed={c.get('suppressed', 0)}"
              + (f" firing: {', '.join(k or '-' for k in r['firing'])}" if r["firing"] else ""))
    for al in st["alerts"]:
        print(f"ALERT {al['rule']} x{al['count']}: {al['text'][:100]}")
    for w in st.get("warnings") or []:
        print(f"warning: {w}")


class FeedsPlugin(Plugin):
    name = "feeds"
    description = "Local feeds and rules that wake agents"
    requires = ("schedules",)
    service = "feeds"
    config_keys = ("feeds", "feed_rules")
    state_dirs = ("feeds",)
    agent_keys = ("feeds",)
    tool_names = frozenset({"feed_status", "feed_read", "feed_action"})
    nav = {"id": "tfeeds", "label": "feeds"}
    static = ("/plugins/feeds", Path(__file__).parent / "static")
    ui = ("ui.js",)

    @classmethod
    def add_cli(cls, sub, url) -> list[str]:
        fe = sub.add_parser("feed", help="local feeds: status, or emit an event (from cron jobs, git hooks, CI)")
        fsub = fe.add_subparsers(dest="feed_cmd", required=True)
        fst = fsub.add_parser("status", help="feeds, rules and open alerts of the running server")
        fst.add_argument("--url", default=url, help="server address (XQO_URL)")
        fem = fsub.add_parser("emit", help="send an event to a push feed, e.g. after a backup: "
                                           "backup.sh; xqo feed emit backup --status $?")
        fem.add_argument("name", help="the push feed")
        fem.add_argument("--kind", default="event")
        fem.add_argument("--severity", choices=["debug", "info", "warning", "error", "critical"],
                         help="default: error when --status is not 0, else info")
        fem.add_argument("--key", help="what makes two events the same (for dedupe and incidents)")
        fem.add_argument("--status", type=int, help="an exit code, as the field status")
        fem.add_argument("--field", action="append", metavar="NAME=VALUE", help="a field (repeatable)")
        fem.add_argument("--text", help="free text, '-' reads standard input (the last 2000 characters are sent)")
        fem.add_argument("--token-env", help="environment variable with the feed's token (default XQO_FEED_TOKEN)")
        fem.add_argument("--url", default=url, help="server address (XQO_URL)")
        return ["feed"]

    @classmethod
    def run_cli(cls, a) -> None:
        _feed(a)

    def agent_config(self, agent, a: dict, dept_cfg) -> None:
        agent.feeds = parse_access(a.get("feeds", (dept_cfg or {}).get("default_feeds")),
                                   f"agent {a['name']!r}")  # none|read|wake|act

    def configure(self, ctx, cfg):
        hub = FeedHub.from_config(ctx.bus, cfg, ctx.data_root)
        if hub:
            ctx.bus.feeds = hub  # register_schedules and check_agents look it up on the bus
            problems = hub.check_agents()
            if problems:
                raise ValueError("; ".join(problems))
            hub.register_schedules()
        return hub

    def check_agents(self) -> list[str]:
        self.obj.warnings = self.obj.check_agents()
        return []

    async def start(self) -> None:
        sc = self.ctx.get("schedules")
        if sc and sc.running:  # only where the scheduler runs, so once per data dir
            await self.obj.start()
        else:
            self.obj.warnings = self.obj.warnings + ["feeds are not watched in this process: the scheduler runs elsewhere"]

    async def stop(self) -> None:
        if self.obj.running:
            await self.obj.stop()

    def status(self) -> dict:
        return {"sources": sorted(self.obj.sources), "rules": sorted(self.obj.rules)}

    def banner(self) -> list[str]:
        lst = self.obj.listen
        return [f"Feeds: {', '.join(self.obj.sources) or 'none'}; rules: {', '.join(self.obj.rules) or 'none'}"
                + (f"  (push on {lst.get('socket') or str(lst.get('host', '127.0.0.1')) + ':' + str(lst['port'])})"
                   if lst.get("socket") or lst.get("port") else "")]

    def tool_defs(self, agent) -> list[dict]:
        return self.obj.tool_defs(agent)

    async def call_tool(self, task, agent, name: str, inp: dict) -> str:
        return await self.obj.tool(task, agent, name, inp)

    def allows(self, task, agent, tool: str) -> bool:
        return self.obj.allows(task, agent, tool)

    def deny_message(self, tool: str) -> str:
        return (f"{tool} is not available in this task: the feed rule that started it runs in "
                "report mode (or does not list this tool)")

    def filter_tools(self, task, agent, tools, servers):
        return self.obj.filter_tools(task, agent, tools, servers)

    def routes(self, app, bus) -> None:
        # ---- local feeds (feeds/) -------------------------------------------------------
        def hub():
            f = getattr(bus, "feeds", None)
            if not f:
                raise HTTPException(404, "no feeds configured (\"feeds\" in agents.json)")
            return f

        @app.get("/api/feeds")
        async def feeds_status():
            f = getattr(bus, "feeds", None)
            return {"enabled": bool(f), **(f.status() if f else {})}

        @app.get("/api/feeds/{name}/events")
        async def feed_events(name: str, limit: int = 50):
            try:
                return hub().recent(name, limit)
            except KeyError as e:
                raise HTTPException(404, str(e.args[0]))

        @app.post("/api/feeds/{name}/events")
        async def feed_push(name: str, request: Request):
            """Events from scripts, cron, git hooks and CI ("type": "push" feeds; token or signature required)."""
            status, body = hub().receive(name, dict(request.headers), await request.body())
            return JSONResponse(body, status)

        @app.post("/api/feeds/rules/{name}/{op}")
        async def feed_rule_op(name: str, op: str, request: Request):
            f = hub()
            try:
                body = await request.json() if (await request.body()) else {}
            except ValueError:
                raise HTTPException(400, "body is not JSON")
            try:
                if op == "mute":
                    return f.mute(name, float(body.get("minutes") or 60))
                if op == "unmute":
                    return f.mute(name, None)
                if op == "test":
                    return f.test(name, body if isinstance(body, dict) else {})
            except KeyError as e:
                raise HTTPException(404, str(e.args[0]))
            except (TypeError, ValueError) as e:
                raise HTTPException(400, str(e))
            raise HTTPException(404, f"unknown operation {op}")

        @app.post("/api/feeds/alerts/{alert_id}/ack")
        async def feed_ack(alert_id: str):
            try:
                return {"acked": hub().ack(None if alert_id == "all" else alert_id)}
            except KeyError as e:
                raise HTTPException(404, str(e.args[0]))
