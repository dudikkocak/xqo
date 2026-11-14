"""Schedules plugin: scheduled and repeating tasks (cron, every, at; feed rules run through it too).

On by default; turn it off with "plugins": {"disable": ["schedules"]}. Configuration: "schedules" in agents.json
(see scheduler.py). The loop runs once per data dir, inside `xqo serve`."""
from __future__ import annotations

from pathlib import Path

import sys

from fastapi import HTTPException, Query
from pydantic import BaseModel, Field

from xqo.api import client_api as _api
from xqo.api import Plugin
from xqo.api import AttachmentIn, decode_attachments as decode
from .scheduler import Scheduler


class ScheduleIn(BaseModel):
    """A schedule (scheduler.py); fields left out keep their value on PATCH."""
    model_config = {"extra": "forbid"}
    name: str | None = Field(default=None, max_length=120)
    prompt: str | None = Field(default=None, max_length=100_000)
    agent: str | None = Field(default=None, max_length=100)
    enabled: bool | None = None
    trigger: dict | None = None
    every: str | float | None = None
    cron: str | None = Field(default=None, max_length=200)
    at: str | float | None = None
    tz: str | None = Field(default=None, max_length=64)
    overlap: str | None = None
    missed: str | None = None
    grace: str | float | None = None
    limits: dict | None = None
    unattended: bool | None = None
    max_failures: int | None = None
    end: dict | None = None
    archive_after: str | float | None = None
    attachments: list[AttachmentIn] = Field(default_factory=list, max_length=1000)


def _schedule_line(s: dict) -> str:
    state = ("finished" if s.get("finished") else "paused" if not s["enabled"] else
             f"next {s['next_run'][:16].replace('T', ' ')} UTC" if s.get("next_run") else "no next run")
    last = f"last {s['last_status']} {(s.get('last_run') or '')[:16].replace('T', ' ')}" if s.get("last_run") else "never ran"
    return (f"{s['id']:<18} {s['name'][:32]:<32} {s['summary'][:28]:<28} {s.get('agent') or '(default)':<14} "
            f"{state:<26} {last}" + (f"  [{s['paused_reason']}]" if s.get("paused_reason") else ""))


def _schedule(a) -> None:
    """schedule list / add / pause / resume / run / rm / runs: through the running server."""
    try:
        c = a.schedule_cmd or "list"
        if c == "list":
            r = _api(a.url, "GET", "/api/schedules")
            if r.get("warning"):
                print(f"warning: {r['warning']}")
            for s in r["items"]:
                print(_schedule_line(s))
            if not r["items"]:
                print("no schedules (add one: xqo schedule add NAME PROMPT --cron '0 9 * * 1-5')")
        elif c == "add":
            body = {"name": a.name, "prompt": a.prompt, "agent": a.agent, "overlap": a.overlap, "missed": a.missed,
                    "unattended": not a.attended, "max_failures": a.max_failures}
            for k in ("every", "cron", "at", "tz", "archive_after"):
                if getattr(a, k):
                    body[k] = getattr(a, k)
            lim = {k: getattr(a, k) for k in ("max_turns", "max_tokens", "max_cost_usd") if getattr(a, k)}
            if a.honor_no_limits:
                lim["honor_no_limits"] = True
            if lim:
                body["limits"] = lim
            end = {k: v for k, v in (("after_runs", a.runs), ("until", a.until)) if v}
            if end:
                body["end"] = end
            if a.paused:
                body["enabled"] = False
            print(_schedule_line(_api(a.url, "POST", "/api/schedules", body)))
        elif c in ("pause", "resume"):
            for sid in a.ids:
                print(_schedule_line(_api(a.url, "POST", f"/api/schedules/{sid}/{c}")))
        elif c == "run":
            for sid in a.ids:
                r = _api(a.url, "POST", f"/api/schedules/{sid}/run")
                print(f"{r['schedule']['name']}: started {r['task_id']}" if r.get("task_id")
                      else f"{r['schedule']['name']}: not started (see the dashboard)")
        elif c == "rm":
            for sid in a.ids:
                _api(a.url, "DELETE", f"/api/schedules/{sid}")
                print(f"{sid}: deleted")
        elif c == "runs":
            for e in _api(a.url, "GET", f"/api/schedules/{a.id}/runs?limit={a.limit}"):
                print(f"{e['ts'][:19].replace('T', ' ')}  {e['kind']:<18} {e.get('task_id') or '':<18} {e['content'][:90]}")
    except RuntimeError as e:
        sys.exit(str(e))
    except ConnectionError:
        sys.exit(f"no server answering at {a.url}: schedules are managed by the running server")


class SchedulesPlugin(Plugin):
    name = "schedules"
    description = "Scheduled and repeating tasks (cron, every, at)"
    service = "scheduler"  # also reachable as bus.scheduler; other plugins use ctx.get("schedules")
    config_keys = ("schedules",)
    skip_files = ("scheduler.lock",)
    nav = {"id": "tsched", "label": "schedules"}
    static = ("/plugins/schedules", Path(__file__).parent / "static")
    ui = ("ui.js",)

    @classmethod
    def add_cli(cls, sub, url) -> list[str]:
        sc = sub.add_parser("schedule", help="scheduled and repeating tasks: list, add, pause, resume, run, rm, runs")
        sc.add_argument("--url", default=url, help="server address (XQO_URL)")
        ssub = sc.add_subparsers(dest="schedule_cmd")
        ssub.add_parser("list", help="all schedules with their next and last run")
        sa = ssub.add_parser("add", help="add a schedule (give one of --cron, --every, --at)")
        sa.add_argument("name")
        sa.add_argument("prompt")
        when = sa.add_mutually_exclusive_group(required=True)
        when.add_argument("--cron", help="5-field cron, e.g. '0 2 * * 1-5' (minute hour day month weekday)")
        when.add_argument("--every", help="interval, e.g. 30m, 6h, 1d")
        when.add_argument("--at", help="one run at a date/time, e.g. 2026-11-01T09:00")
        sa.add_argument("--tz", help="time zone of the times, e.g. Europe/Berlin (default: the server's)")
        sa.add_argument("--agent", help="entry agent (default: the first one)")
        sa.add_argument("--overlap", choices=["skip", "queue", "parallel"], default="skip",
                        help="when the previous run is still going (default skip)")
        sa.add_argument("--missed", choices=["run_once", "skip"], default="run_once",
                        help="a run missed while the server was down (default run_once, within an hour)")
        sa.add_argument("--max-turns", type=int)
        sa.add_argument("--max-tokens", type=int)
        sa.add_argument("--max-cost-usd", type=float, help="cancel a run when its estimated cost reaches this")
        sa.add_argument("--honor-no-limits", action="store_true", help="the dashboard's No limits hour applies too")
        sa.add_argument("--attended", action="store_true", help="questions wait for an answer like other tasks")
        sa.add_argument("--max-failures", type=int, default=3, help="pause after this many failed runs in a row")
        sa.add_argument("--runs", type=int, help="finish after this many runs")
        sa.add_argument("--until", help="finish after this date/time")
        sa.add_argument("--archive-after", help="archive finished runs older than this, e.g. 7d")
        sa.add_argument("--paused", action="store_true", help="create it paused")
        for kind, what in (("pause", "pause schedule(s)"), ("resume", "resume schedule(s)"),
                           ("run", "start a run now"), ("rm", "delete schedule(s)")):
            x = ssub.add_parser(kind, help=what)
            x.add_argument("ids", nargs="+", metavar="SCHEDULE_ID")
        sr = ssub.add_parser("runs", help="recent runs of a schedule")
        sr.add_argument("id", metavar="SCHEDULE_ID")
        sr.add_argument("--limit", type=int, default=20)
        return ["schedule"]

    @classmethod
    def run_cli(cls, a) -> None:
        _schedule(a)

    def configure(self, ctx, cfg):
        return Scheduler(ctx.bus, ctx.data_root, cfg.get("schedules"))

    async def start(self) -> None:
        if not self.obj.start():
            print(f"warning: {self.obj.lock_error}", file=sys.stderr)

    async def stop(self) -> None:
        await self.obj.stop()

    def status(self) -> dict:
        return {"running": self.obj.running, "warning": self.obj.lock_error, "count": len(self.obj.schedules)}

    def routes(self, app, bus) -> None:
        # ---- schedules (scheduler.py) -----------------------------------------
        def schedule_call(fn, *args):
            try:
                return fn(*args)
            except KeyError as e:
                raise HTTPException(404, str(e.args[0] if e.args else e))
            except PermissionError as e:
                raise HTTPException(403, str(e))
            except ValueError as e:
                raise HTTPException(422, str(e))

        def schedule_body(body: ScheduleIn) -> tuple[dict, list[dict]]:
            d = body.model_dump(exclude_unset=True)
            d.pop("attachments", None)
            return d, decode(body.attachments)

        @app.get("/api/schedules")
        async def schedules():
            sc = bus.plugins.service("schedules")
            return {"running": sc.running, "warning": sc.lock_error, "items": sc.list()}

        @app.post("/api/schedules")
        async def schedule_create(body: ScheduleIn):
            d, files = schedule_body(body)
            return schedule_call(bus.plugins.service("schedules").create, d, files)

        @app.get("/api/schedules/{sid}")
        async def schedule_get(sid: str):
            return schedule_call(lambda: bus.plugins.service("schedules").view(bus.plugins.service("schedules").get(sid)))

        @app.patch("/api/schedules/{sid}")
        async def schedule_update(sid: str, body: ScheduleIn):
            d, files = schedule_body(body)
            if files:
                raise HTTPException(422, "attachments are set when a schedule is created")
            return schedule_call(bus.plugins.service("schedules").update, sid, d)

        @app.delete("/api/schedules/{sid}")
        async def schedule_delete(sid: str):
            return schedule_call(bus.plugins.service("schedules").delete, sid)

        @app.post("/api/schedules/{sid}/pause")
        async def schedule_pause(sid: str):
            return schedule_call(bus.plugins.service("schedules").set_enabled, sid, False)

        @app.post("/api/schedules/{sid}/resume")
        async def schedule_resume(sid: str):
            return schedule_call(bus.plugins.service("schedules").set_enabled, sid, True)

        @app.post("/api/schedules/{sid}/run")
        async def schedule_run(sid: str):
            schedule_call(bus.plugins.service("schedules").get, sid)
            return await bus.plugins.service("schedules").run_now(sid, "user")

        @app.get("/api/schedules/{sid}/runs")
        async def schedule_runs(sid: str, limit: int = Query(100, ge=1, le=1000)):
            return schedule_call(bus.plugins.service("schedules").runs, sid, limit)
