"""Flows plugin: visual workflows of xqo agents, drawn in a FlowGram.AI editor and run by xqo.

Turn it on with "flows": {} in agents.json (see service.py for the keys, workers.py for remote workers). The
editor is at /plugins/flows/editor.html (the "flows" link in the dashboard header). Runs start from the editor,
the API (POST /api/flows/<name>/run), the CLI (xqo flows run <name>) or an agent's run_flow tool."""
from __future__ import annotations

import sys
import time
from pathlib import Path

from fastapi import HTTPException, Query, Request
from pydantic import BaseModel, Field

from xqo.api import Plugin
from xqo.api import client_api as _api

from .engine import FINAL, Engine, Run, Steps
from .model import FlowError, validate
from .service import FlowService
from .steps import BusSteps, run_task
from .workers import Dispatcher, create_worker_app, hook_response

__all__ = ["FlowsPlugin", "FlowService", "Engine", "Run", "Steps", "BusSteps", "run_task", "validate",
           "FlowError", "Dispatcher", "FINAL"]

STATIC = Path(__file__).parent / "static"

RUN_FLOW_TOOL = {
    "name": "run_flow",
    "description": "Start one of the HQ's saved visual flows (a fixed sequence of agent steps, conditions and "
                   "approvals) and wait for its result. Use it when the user asks for something a listed flow does.",
    "input_schema": {"type": "object",
                     "properties": {"name": {"type": "string", "description": "the flow's name"},
                                    "inputs": {"type": "object", "additionalProperties": {"type": "string"},
                                               "description": "the flow's inputs by name"}},
                     "required": ["name"], "additionalProperties": False},
}


class RunIn(BaseModel):
    model_config = {"extra": "forbid"}
    inputs: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    labels: list[str] = Field(default_factory=list, max_length=20)
    worker: str = Field(default="", max_length=100)
    remote: bool = False


class ApproveIn(BaseModel):
    model_config = {"extra": "forbid"}
    node: str = Field(max_length=64)
    approve: bool = True


def _call(fn, *args):
    try:
        return fn(*args)
    except KeyError as e:
        raise HTTPException(404, str(e.args[0] if e.args else e))
    except (FlowError, ValueError) as e:
        raise HTTPException(422, str(e))


def _line(r: dict) -> str:
    return (f"{r['id']:<16} {r['flow'][:24]:<24} {r['status']:<11} {r['started'][:19].replace('T', ' ')}  "
            f"{r.get('where', 'hq'):<16} {(r.get('error') or r.get('result') or '')[:60]!r}")


def _cli(a) -> None:
    url = a.url
    try:
        c = a.flows_cmd or "list"
        if c == "list":
            rows = _api(url, "GET", "/api/flows")
            for f in rows:
                last = f.get("last_run") or {}
                print(f"{f['name']:<24} {f['title'][:36]:<36} {f.get('nodes', 0):>3} nodes  "
                      f"inputs: {', '.join(f.get('inputs') or []) or '-':<20} last: {last.get('status', '-')}")
            if not rows:
                print("no flows (draw one on the Flows page, or: xqo flows import FILE.json)")
        elif c == "show":
            import json
            print(json.dumps(_api(url, "GET", f"/api/flows/{a.name}"), indent=2))
        elif c == "import":
            import json
            doc = json.loads(Path(a.file).read_text(encoding="utf-8"))
            name = a.name or doc.get("name") or Path(a.file).stem
            _api(url, "PUT", f"/api/flows/{name}", doc)
            print(f"saved flow {name}")
        elif c == "rm":
            _api(url, "DELETE", f"/api/flows/{a.name}")
            print(f"deleted flow {a.name}")
        elif c == "run":
            inputs = {}
            for kv in a.input:
                k, sep, v = kv.partition("=")
                if not sep:
                    sys.exit(f"--input takes NAME=VALUE, got {kv!r}")
                inputs[k] = v
            body = {"inputs": inputs, "labels": [x for x in (a.labels or "").split(",") if x],
                    "worker": a.worker or "", "remote": bool(a.remote)}
            r = _api(url, "POST", f"/api/flows/{a.name}/run", body)
            print(f"started {r['id']} ({r['flow']})")
            if a.no_wait:
                return
            seen: dict[str, str] = {}
            while True:
                r = _api(url, "GET", f"/api/flows/runs/{r['id']}")
                for nid, st in r["nodes"].items():
                    if seen.get(nid) != st["status"] and st.get("type") != "start" and st["status"] != "pending":
                        seen[nid] = st["status"]
                        print(f"  {st.get('title', nid):<28} {st['status']}"
                              + (f"  ({st.get('question')})  -> xqo flows approve {r['id']} {nid} [--reject]"
                                 if st["status"] == "waiting" else ""))
                if r["status"] in FINAL:
                    break
                time.sleep(1)
            print(f"\n[{r['status']}] " + (r.get("result") or r.get("error") or ""))
        elif c == "runs":
            for r in _api(url, "GET", f"/api/flows/runs?limit={a.limit}" + (f"&flow={a.name}" if a.name else "")):
                print(_line(r))
        elif c == "cancel":
            r = _api(url, "POST", f"/api/flows/runs/{a.run_id}/cancel")
            print(f"{r['id']}: {r['status']}")
        elif c == "resume":
            r = _api(url, "POST", f"/api/flows/runs/{a.run_id}/resume")
            print(f"resumed {r['id']} ({r['flow']}): {r['status']}")
        elif c == "triggers":
            rows = _api(url, "GET", "/api/flows/triggers" + (f"?flow={a.name}" if a.name else ""))
            for t in rows:
                when = t.get("when") or t.get("path") or ""
                print(f"{t['flow'][:20]:<20} {t['id'][:16]:<16} {t['type']:<8} {'on ' if t.get('enabled') else 'off'} "
                      f"{when[:34]:<34} next: {(t.get('next_run') or '-')[:19]:<19} last: {t.get('last_status') or '-'}"
                      + (f"  ({t['warning']})" if t.get("warning") else ""))
            if not rows:
                print("no triggers (add them in the editor's Flow panel or a flow's \"triggers\" list)")
        elif c == "approve":
            r = _api(url, "POST", f"/api/flows/runs/{a.run_id}/approve", {"node": a.node, "approve": not a.reject})
            print(f"{r['run_id']} {r['node']}: {r['answer']}")
        elif c == "workers":
            st = _api(url, "GET", "/api/flows/workers")
            print(f"worker API: {st['listen'] or '(off: set flows.workers.listen)'}   queued jobs: {st['queued']}")
            for w in st["workers"]:
                print(f"  {w['name']:<16} {'online ' if w['online'] else 'offline'} labels: {', '.join(w['labels']) or '-':<20}"
                      f" running: {len(w['running'])}" + ("" if w["token_set"] else f"  ({w['token_env']} not set)"))
        elif c == "token":
            import secrets
            print(secrets.token_urlsafe(32))
    except RuntimeError as e:
        sys.exit(str(e))
    except ConnectionError:
        sys.exit(f"no server answering at {url}: flows are run by the running server (xqo serve)")


class FlowsPlugin(Plugin):
    name = "flows"
    description = "Visual workflows of agents (FlowGram.AI editor), with remote workers"
    service = "flows"
    config_keys = ("flows",)
    state_dirs = ("flows",)
    tool_names = frozenset({"run_flow"})
    static = ("/plugins/flows", STATIC)
    nav = {"id": "tflows", "label": "flows", "href": "/plugins/flows/editor.html", "title": "Visual flows"}
    ui = ("ui.js",)

    @classmethod
    def add_cli(cls, sub, url) -> list[str]:
        p = sub.add_parser("flows", help="visual flows: list, show, import, rm, run, runs, cancel, resume, "
                                           "approve, triggers, workers, token")
        p.add_argument("--url", default=url, help="server address (XQO_URL)")
        s = p.add_subparsers(dest="flows_cmd")
        s.add_parser("list", help="saved flows")
        x = s.add_parser("show", help="print a flow as JSON")
        x.add_argument("name")
        x = s.add_parser("import", help="save a flow from a JSON file")
        x.add_argument("file")
        x.add_argument("--name", help="save under this name (default: the file's name field or file name)")
        x = s.add_parser("rm", help="delete a flow")
        x.add_argument("name")
        x = s.add_parser("run", help="start a run and follow it")
        x.add_argument("name")
        x.add_argument("--input", "-i", action="append", default=[], metavar="NAME=VALUE")
        x.add_argument("--remote", action="store_true", help="run the whole flow on a worker (any worker)")
        x.add_argument("--labels", help="run the whole flow on a worker with these labels (comma-separated)")
        x.add_argument("--worker", help="run the whole flow on this worker")
        x.add_argument("--no-wait", action="store_true", help="print the run id and return")
        x = s.add_parser("runs", help="recent runs")
        x.add_argument("name", nargs="?")
        x.add_argument("--limit", type=int, default=20)
        x = s.add_parser("cancel", help="cancel a run")
        x.add_argument("run_id")
        x = s.add_parser("resume", help="continue an interrupted, failed or cancelled run (done steps are kept)")
        x.add_argument("run_id")
        x = s.add_parser("triggers", help="schedule and webhook triggers, with their next and last runs")
        x.add_argument("name", nargs="?")
        x = s.add_parser("approve", help="answer a run's approval step")
        x.add_argument("run_id")
        x.add_argument("node")
        x.add_argument("--reject", action="store_true")
        s.add_parser("workers", help="remote workers and the job queue")
        s.add_parser("token", help="print a new random token for a worker")
        return ["flows"]

    @classmethod
    def run_cli(cls, a) -> None:
        _cli(a)

    def configure(self, ctx, cfg):
        if "flows" not in cfg:
            return None
        return FlowService(ctx.bus, cfg.get("flows"), ctx.data_root)

    async def start(self) -> None:
        await self.obj.start()

    async def stop(self) -> None:
        await self.obj.stop()

    def status(self) -> dict:
        return self.obj.status()

    def banner(self) -> list[str]:
        out = ["Flows: /plugins/flows/editor.html"]
        d = self.obj.dispatcher
        if d.listen:
            out.append(f"Flows worker API and webhooks: http://{d.listen[0]}:{d.listen[1]}/flows/v1/  "
                       f"({len(d.workers)} worker{'s' if len(d.workers) != 1 else ''} listed)")
        return out

    def side_servers(self):
        d = self.obj.dispatcher
        if not d.listen:
            return []
        return [(create_worker_app(d, self.obj), d.listen[0], d.listen[1])]

    def tool_defs(self, agent) -> list[dict]:
        return [RUN_FLOW_TOOL] if self.obj.may_run(agent) else []

    def prompt_note(self, agent) -> str:
        return self.obj.describe_for_agent() if self.obj.may_run(agent) else ""

    async def call_tool(self, task, agent, name: str, inp: dict) -> str:
        return await self.obj.tool_run(task, agent, inp or {})

    def routes(self, app, bus) -> None:
        def svc() -> FlowService:
            s = bus.plugins.service("flows")
            if s is None:
                raise HTTPException(404, "flows are off")
            return s

        @app.get("/api/flows")
        async def flows():
            return svc().list()

        # runs and workers first: "runs" / "workers" are not flow names (model.RESERVED)
        @app.get("/api/flows/runs")
        async def runs(flow: str | None = None, limit: int = Query(50, ge=1, le=500)):
            return svc().list_runs(flow, limit)

        @app.get("/api/flows/runs/{run_id}")
        async def run_get(run_id: str):
            s = svc()
            return _call(lambda: s.view(s.get_run(run_id)))

        @app.post("/api/flows/runs/{run_id}/resume")
        async def run_resume(run_id: str):
            s = svc()
            _call(s.get_run, run_id)
            try:
                return s.view(s.resume(run_id, "user"))
            except ValueError as e:
                raise HTTPException(409, str(e))

        @app.post("/api/flows/runs/{run_id}/cancel")
        async def run_cancel(run_id: str):
            s = svc()
            _call(s.get_run, run_id)
            try:
                return s.view(await s.cancel(run_id))
            except ValueError as e:
                raise HTTPException(409, str(e))

        @app.post("/api/flows/runs/{run_id}/approve")
        async def run_approve(run_id: str, body: ApproveIn):
            return _call(svc().approve, run_id, body.node, body.approve, "user")

        @app.get("/api/flows/approvals")
        async def approvals():
            return svc().pending_approvals()

        @app.get("/api/flows/triggers")
        async def triggers(flow: str | None = None):
            return svc().trigger_rows(flow)

        @app.post("/api/flows/hooks/{flow}/{tid}")
        async def hook(flow: str, tid: str, request: Request):
            return await hook_response(svc(), flow, tid, request)

        @app.get("/api/flows/workers")
        async def workers():
            return svc().dispatcher.status()

        @app.get("/api/flows/{name}")
        async def flow_get(name: str):
            return _call(svc().get, name)

        @app.put("/api/flows/{name}")
        async def flow_put(name: str, body: dict):
            s = svc()
            doc = _call(s.save, name, body)
            return {**doc, "warnings": s.warnings(doc)}

        @app.delete("/api/flows/{name}")
        async def flow_delete(name: str):
            return _call(svc().delete, name)

        @app.post("/api/flows/{name}/run")
        async def flow_run(name: str, body: RunIn):
            s = svc()
            run = _call(s.start_run, name, body.inputs, "user", body.labels, body.worker, body.remote)
            return s.view(run)
