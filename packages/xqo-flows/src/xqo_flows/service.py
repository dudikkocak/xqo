"""The flows service on the HQ: saved flows, runs, approvals and the worker queue.

Files (under the data dir's artifacts/, in every backup):
    flows/<name>.json         a flow as the editor saved it (model.py)
    flows/runs/<run_id>.json  one run: status, inputs, result, every node's state and output
    flows/queue/jobs.json     the remote worker queue (workers.py)
    flows/queue/triggers.json schedule triggers' next / last runs (triggers.py)

Configuration ("flows" in agents.json; the key turns the plugin on, {} is enough):
    "flows": {
      "max_running": 20,                 # runs going at the same time on this HQ
      "approval_timeout": 86400,         # seconds an approval waits before its default answer (0 = forever)
      "agents_may_run": ["planner"],     # agents that get the run_flow tool ("*" = all; default: none)
      "tool_timeout": 3600,              # how long run_flow waits for a run
      "examples": true,                  # write the example flows on first start
      "listen": "0.0.0.0:8200",          # public listener: the worker API and webhooks (XQO_FLOWS_LISTEN)
      "workers": {...}                   # remote workers (workers.py)
    }"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import time
from pathlib import Path

from xqo.api import Event, utcnow

from .engine import FINAL, Engine, Run, wait_for
from .triggers import iso as trig_iso
from .model import FlowError, has_types, inputs_of, validate
from .steps import BusSteps
from . import triggers as trig
from .workers import Dispatcher, parse_listen

EXAMPLES = Path(__file__).parent / "examples"
KEYS = {"max_running", "approval_timeout", "agents_may_run", "tool_timeout", "examples", "workers", "listen"}
KEEP_RUNS = 500


class HQSteps(BusSteps):
    where = "the HQ"

    def __init__(self, service: "FlowService"):
        super().__init__(service.bus, service.changed)
        self.svc = service

    async def remote(self, run: Run, node: dict, prompt: str) -> dict:
        d = node["data"]
        job = self.svc.dispatcher.submit("task", {"agent": d.get("agent", ""), "prompt": prompt,
                                                  "title": f"{run.flow} · {d.get('title') or node['id']}",
                                                  "unattended": d.get("unattended", True) is not False},
                                         d.get("labels") or [], d.get("worker") or "", run.id, node["id"])
        run.nodes[node["id"]]["job_id"] = job.id
        self.svc.changed(run, node["id"])
        try:
            res = await wait_for(self.svc.dispatcher.future(job.id), d.get("timeout") or None)
        except asyncio.TimeoutError:
            self.svc.dispatcher.cancel(job.id, "timed out on the HQ")
            raise FlowError(f"no result from a worker within {d['timeout']}s")
        except asyncio.CancelledError:
            self.svc.dispatcher.cancel(job.id, "the flow run was cancelled")
            raise
        run.nodes[node["id"]]["worker"] = res.get("worker", "")
        return {"result": res.get("result", ""), "status": "done" if res["status"] == "done" else "error",
                "error": res.get("error", ""), "task_id": res.get("task_id", ""), "worker": res.get("worker", "")}

    async def approval(self, run: Run, node: dict, question: str) -> str:
        return await self.svc.wait_approval(run, node, question)


class FlowService:
    def __init__(self, bus, cfg: dict | None, data_root: Path):
        cfg = cfg or {}
        if not isinstance(cfg, dict):
            raise ValueError("\"flows\" must be an object")
        unknown = set(cfg) - KEYS
        if unknown:
            raise ValueError(f"flows: unknown key(s) {', '.join(sorted(unknown))}; known: {', '.join(sorted(KEYS))}")
        self.bus = bus
        self.dir = Path(data_root) / "flows"
        self.runs_dir = self.dir / "runs"
        self.max_running = int(cfg.get("max_running", 20))
        self.approval_timeout = float(cfg.get("approval_timeout", 86400)) or None
        may = cfg.get("agents_may_run", [])
        self.agents_may_run = may if may == "*" else set(may or [])
        self.tool_timeout = float(cfg.get("tool_timeout", 3600))
        self.examples = cfg.get("examples", True) is not False
        try:
            self.dispatcher = Dispatcher(cfg.get("workers"), self.dir / "queue", getattr(bus, "hq_name", "hq") or "hq")
        except FlowError as e:
            raise ValueError(str(e)) from None
        self.dispatcher.progress_hook(self._progress)
        try:
            self.listen = parse_listen(os.getenv("XQO_FLOWS_LISTEN") or cfg.get("listen")
                                       or (cfg.get("workers") or {}).get("listen") or "")
        except FlowError as e:
            raise ValueError(str(e)) from None
        self.dispatcher.listen = self.listen
        self.trigger_state = trig.TriggerState(self.dir / "queue" / "triggers.json")
        self.ratelimit = trig.RateLimit()
        self._trigger_task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self.steps = HQSteps(self)
        self.engine = Engine(self.steps, self.changed)
        self.runs: dict[str, Run] = {}
        self.jobs: dict[str, asyncio.Task] = {}
        self.approvals: dict[tuple[str, str], dict] = {}  # (run, node) -> {question, future, since}
        self._emitted: dict[tuple[str, str], str] = {}
        self._stopping = False

    # ---- lifecycle ----
    async def start(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        if self.examples and not any(self.dir.glob("*.json")) and not (self.dir / ".examples").exists():
            for f in sorted(EXAMPLES.glob("*.json")):
                shutil.copy(f, self.dir / f.name)
            (self.dir / ".examples").write_text(utcnow(), encoding="utf-8")
        for f in sorted(self.runs_dir.glob("*.json")):
            try:
                run = Run.from_dict(json.loads(f.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError):
                continue
            if run.status not in FINAL:
                run.status, run.ended = "interrupted", utcnow()
                run.error = run.error or "the server stopped while this run was going"
                for st in run.nodes.values():
                    if st.get("status") in ("running", "waiting"):
                        st["status"] = "interrupted"
                self._save_run(run)
            self.runs[run.id] = run
        self._prune()
        await self.dispatcher.start()
        self._trigger_task = asyncio.ensure_future(self._trigger_loop())

    async def stop(self) -> None:
        self._stopping = True
        if self._trigger_task:
            self._trigger_task.cancel()
            with contextlib.suppress(BaseException):
                await self._trigger_task
        for job in list(self.jobs.values()):
            job.cancel()
        if self.jobs:
            await asyncio.wait(set(self.jobs.values()), timeout=30)
        await self.dispatcher.stop()

    # ---- flows ----
    def _path(self, name: str) -> Path:
        validate({"name": name, "nodes": [{"id": "s", "type": "start"}]})  # checks the name
        return self.dir / f"{name}.json"

    def list(self) -> list[dict]:
        out = []
        for f in sorted(self.dir.glob("*.json")):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                out.append({"name": f.stem, "title": f.stem, "error": "unreadable file"})
                continue
            last = max((r for r in self.runs.values() if r.flow == f.stem), key=lambda r: r.started, default=None)
            out.append({"name": f.stem, "title": d.get("title") or f.stem, "description": d.get("description", ""),
                        "nodes": len(d.get("nodes") or []), "updated": d.get("updated", ""),
                        "inputs": [i.get("name") for i in next((n.get("data", {}).get("inputs", [])
                                                               for n in d.get("nodes") or []
                                                               if n.get("type") == "start"), [])],
                        "last_run": {"id": last.id, "status": last.status, "started": last.started} if last else None})
        return out

    def get(self, name: str) -> dict:
        p = self._path(name)
        if not p.is_file():
            raise KeyError(f"no flow named {name}")
        return json.loads(p.read_text(encoding="utf-8"))

    def save(self, name: str, doc: dict) -> dict:
        doc = validate({**doc, "name": name}, name)
        doc["updated"] = utcnow()
        p = self._path(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(p)
        self._wake.set()  # triggers may have changed
        return doc

    def delete(self, name: str) -> dict:
        p = self._path(name)
        if not p.is_file():
            raise KeyError(f"no flow named {name}")
        p.unlink()
        return {"deleted": name}

    def warnings(self, doc: dict, triggers: bool = True) -> list[str]:
        """Things that make a run fail at once (checked before it starts), and with triggers=True also triggers
        that cannot fire (shown when the flow is saved)."""
        out = []
        for t in (doc.get("triggers") or []) if triggers else []:
            if t["type"] == "schedule" and t["enabled"]:
                why = trig.check_schedule(t)
                if why:
                    out.append(why)
            if t["type"] == "webhook" and t["enabled"] and not os.getenv(t["secret_env"]):
                out.append(f"trigger {t['id']}: set {t['secret_env']} on this HQ, or the webhook refuses every call")
        for n in doc["nodes"]:
            a = n["data"].get("agent")
            if n["type"] == "agent" and a and not self.bus.agent_id(a):
                out.append(f"node {n['id']}: unknown agent {a!r}")
            if n["type"] == "remote" and not self.dispatcher.matching(n["data"].get("labels") or [],
                                                                       n["data"].get("worker") or ""):
                out.append(f"node {n['id']}: no worker in flows.workers.allow has the labels "
                           f"{', '.join(n['data'].get('labels') or []) or '(any)'}")
        return out

    # ---- runs ----
    def _save_run(self, run: Run) -> None:
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        p = self.runs_dir / f"{run.id}.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(run.to_dict(), ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)

    def _prune(self) -> None:
        done = sorted((r for r in self.runs.values() if r.status in FINAL), key=lambda r: r.started)
        for r in done[:-KEEP_RUNS]:
            self.runs.pop(r.id, None)
            with contextlib.suppress(OSError):
                (self.runs_dir / f"{r.id}.json").unlink()

    def changed(self, run: Run, nid: str | None) -> None:
        self._save_run(run)
        if nid is None:
            return
        st = run.nodes.get(nid, {})
        status = st.get("status")
        if self._emitted.get((run.id, nid)) == status or st.get("type") == "start":
            return
        self._emitted[(run.id, nid)] = status
        self.bus.emit(Event("flow_node", "flows", None, st.get("task_id") or None, None,
                            f"{run.flow} › {st.get('title', nid)}: {status}",
                            {"run_id": run.id, "flow": run.flow, "node": nid, "status": status}))

    def _run_event(self, run: Run) -> None:
        self.bus.emit(Event("flow_run", "flows", None, None, None,
                            f"flow {run.flow}: {run.status}" + (f" ({run.error[:200]})" if run.error else ""),
                            {"run_id": run.id, "flow": run.flow, "status": run.status, "where": run.where}))

    def running(self) -> list[Run]:
        return [r for r in self.runs.values() if r.status not in FINAL]

    def start_run(self, name: str, inputs: dict | None = None, by: str = "user", labels: list[str] | None = None,
                  worker: str = "", remote: bool = False, trigger: dict | None = None) -> Run:
        """Start a run (returns at once). remote / labels / worker: send the whole flow to a worker."""
        self._can_start()
        doc = validate(self.get(name), name)
        inputs = {str(k): ("" if v is None else str(v)) for k, v in (inputs or {}).items()}
        known = {i["name"] for i in inputs_of(doc)}
        extra = set(inputs) - known
        if extra:
            raise FlowError(f"unknown input(s) {', '.join(sorted(extra))}; this flow takes: "
                            f"{', '.join(sorted(known)) or 'none'}")
        remote = remote or bool(labels) or bool(worker)
        self._check_where(doc, remote)
        run = Run(flow=name, title=doc.get("title", name), inputs=inputs, by=by, trigger=trigger, doc=doc)
        for n in doc["nodes"]:
            run.nodes[n["id"]] = {"status": "pending", "title": n["data"].get("title", n["id"]), "type": n["type"]}
        if remote:
            self._submit_flow_job(run, doc, labels or [], worker)
        self.runs[run.id] = run
        self._save_run(run)
        self._run_event(run)
        self.jobs[run.id] = asyncio.ensure_future(self._go(doc, run))
        self._prune()
        return run

    def _can_start(self) -> None:
        if self._stopping:
            raise FlowError("the server is stopping")
        if getattr(self.bus, "draining", False):
            raise FlowError("the server is draining before a restart: new runs start after it")
        if len(self.running()) >= self.max_running:
            raise FlowError(f"{self.max_running} runs are going already (flows.max_running)")

    def _check_where(self, doc: dict, remote: bool) -> None:
        if remote:
            bad = has_types(doc, "remote")
            if bad:
                raise FlowError("a flow with remote steps cannot run on a worker as a whole "
                                f"({', '.join(bad)}); run it here, where its remote steps go to workers")
        else:
            warn = self.warnings(doc, triggers=False)
            if warn:
                raise FlowError("; ".join(warn))

    def _submit_flow_job(self, run: Run, doc: dict, labels: list[str], worker: str) -> None:
        spec = {"flow": doc, "inputs": run.inputs, "run_id": run.id, "title": run.flow,
                "approval_timeout": self.approval_timeout, "nodes": run.nodes if run.resumed else None}
        job = self.dispatcher.submit("flow", spec, labels, worker, run.id, "")
        run.where, run.job_id = "worker", job.id
        run.remote = {"labels": labels, "worker": worker}

    async def _go(self, doc: dict, run: Run, resume: bool = False) -> None:
        try:
            if run.job_id:
                await self._remote_run(run)
            else:
                await self.engine.run(doc, run, resume=resume)
        except asyncio.CancelledError:
            run.status = "interrupted" if self._stopping else "cancelled"
            run.error = "the server stopped" if self._stopping else (run.error or "cancelled")
            if run.job_id and self._stopping:
                self.dispatcher.cancel(run.job_id, "the HQ stopped")
        except Exception as e:
            run.status, run.error = "error", f"{type(e).__name__}: {e}"
        finally:
            run.ended = run.ended or utcnow()
            for (rid, nid), a in list(self.approvals.items()):
                if rid == run.id:
                    self.approvals.pop((rid, nid), None)
            self._save_run(run)
            self.jobs.pop(run.id, None)
            for k in [k for k in self._emitted if k[0] == run.id]:
                self._emitted.pop(k, None)
            self._run_event(run)

    def resume(self, run_id: str, by: str = "user") -> Run:
        """Continue an interrupted, failed or cancelled run: finished steps keep their results, the others run
        again. A run that went to a worker as a whole goes back to a worker (with the same labels)."""
        run = self.get_run(run_id)
        if run.id in self.jobs or run.status not in ("interrupted", "error", "cancelled"):
            raise ValueError(f"run {run_id} cannot be resumed (status: {run.status}); only interrupted, failed "
                             "or cancelled runs can")
        self._can_start()
        doc = run.doc
        if doc is None:  # a run from before resume existed: use the saved flow if it still has the same steps
            doc = validate(self.get(run.flow), run.flow)
            if {n["id"] for n in doc["nodes"]} != set(run.nodes):
                raise ValueError("the flow changed since this run started; start a new run instead")
        remote = run.where != "hq" and run.where != "worker-local"
        self._check_where(doc, remote)
        run.resumed += 1
        run.status, run.error, run.result, run.ended = "running", "", "", None
        run.by = by
        if remote:
            prev = getattr(run, "remote", None) or {}
            for st in run.nodes.values():
                if st.get("status") != "done":
                    st["status"] = "pending"
            self._submit_flow_job(run, doc, prev.get("labels") or [], prev.get("worker") or "")
        else:
            run.job_id = None
        self._save_run(run)
        self.bus.emit(Event("flow_run", "flows", None, None, None, f"flow {run.flow}: resumed by {by}",
                            {"run_id": run.id, "flow": run.flow, "status": "running", "resumed": run.resumed}))
        self.jobs[run.id] = asyncio.ensure_future(self._go(doc, run, resume=True))
        return run

    async def _remote_run(self, run: Run) -> None:
        try:
            res = await asyncio.shield(self.dispatcher.future(run.job_id))
        except asyncio.CancelledError:
            self.dispatcher.cancel(run.job_id, "the run was cancelled on the HQ")
            raise
        run.status = res["status"] if res["status"] in ("done", "error", "cancelled") else "error"
        run.result, run.error = res.get("result", ""), res.get("error", "")
        run.where = f"worker:{res.get('worker') or '?'}"
        run.ended = utcnow()

    def _progress(self, job, progress: dict) -> None:
        """A worker's heartbeat on a whole-flow job: copy its node states into the HQ's run record, and show the
        approvals it waits for here (the answers go back with the next heartbeat)."""
        run = self.runs.get(job.run_id)
        if not run or job.kind != "flow" or run.job_id != job.id:
            return
        run.where = f"worker:{job.claimed_by}"
        for nid, st in (progress.get("nodes") or {}).items():
            if nid in run.nodes and isinstance(st, dict):
                keep = {k: st[k] for k in ("status", "started", "ended", "output", "error", "task_id", "port",
                                           "question", "answer", "answered_by") if k in st}
                run.nodes[nid].update(keep)
                self.changed(run, nid)
        asked = progress.get("approvals") if isinstance(progress.get("approvals"), dict) else {}
        for nid, a in asked.items():
            key = (run.id, nid)
            if key not in self.approvals and nid not in job.answers and nid in run.nodes:
                self.approvals[key] = {"question": str((a or {}).get("question", ""))[:20000], "future": None,
                                       "job_id": job.id, "since": utcnow()}
                run.nodes[nid].update(status="waiting", question=self.approvals[key]["question"])
                self.changed(run, nid)
        for key in [k for k, a in self.approvals.items() if k[0] == run.id and a.get("job_id") == job.id
                    and k[1] not in asked]:
            self.approvals.pop(key, None)  # answered or timed out on the worker
        run.status = "waiting" if any(k[0] == run.id for k in self.approvals) else "running"

    def view(self, run: Run) -> dict:
        d = run.to_dict()
        d.pop("doc", None)
        d["resumable"] = run.id not in self.jobs and run.status in ("interrupted", "error", "cancelled")
        return d

    def get_run(self, run_id: str) -> Run:
        run = self.runs.get(run_id)
        if not run:
            raise KeyError(f"no run {run_id}")
        return run

    def list_runs(self, flow: str | None = None, limit: int = 50) -> list[dict]:
        rows = sorted((r for r in self.runs.values() if not flow or r.flow == flow), key=lambda r: r.started,
                      reverse=True)[:limit]
        return [{k: v for k, v in r.to_dict().items() if k not in ("nodes", "doc")} |
                {"waiting": [n for n, st in r.nodes.items() if st.get("status") == "waiting"]} for r in rows]

    async def cancel(self, run_id: str) -> Run:
        run = self.get_run(run_id)
        job = self.jobs.get(run_id)
        if not job or job.done():
            raise ValueError(f"run {run_id} is not going (status: {run.status})")
        run.error = "cancelled by the user"
        job.cancel()
        await asyncio.wait({job}, timeout=30)
        return run

    async def wait(self, run_id: str, timeout: float | None = None) -> Run:
        job = self.jobs.get(run_id)
        if job:
            await asyncio.wait({job}, timeout=timeout)
        return self.get_run(run_id)

    # ---- approvals ----
    async def wait_approval(self, run: Run, node: dict, question: str) -> str:
        fut = asyncio.get_running_loop().create_future()
        key = (run.id, node["id"])
        self.approvals[key] = {"question": question, "future": fut, "since": utcnow()}
        run.nodes[node["id"]]["question"] = question
        run.status = "waiting"
        self.changed(run, node["id"])
        timeout = node["data"].get("timeout") or self.approval_timeout
        try:
            answer = await wait_for(fut, timeout)
            by = self.approvals.get(key, {}).get("by", "user")
        except asyncio.TimeoutError:
            answer, by = node["data"].get("default", "reject"), "timeout"
        finally:
            self.approvals.pop(key, None)
            if not any(k[0] == run.id for k in self.approvals):
                run.status = "running"
        run.nodes[node["id"]].update(answer=answer, answered_by=by)
        return answer

    def approve(self, run_id: str, node_id: str, approve: bool, by: str = "user") -> dict:
        a = self.approvals.get((run_id, node_id))
        if not a:
            raise KeyError(f"run {run_id} is not waiting for an approval at {node_id}")
        a["by"] = by
        answer = "approve" if approve else "reject"
        if a.get("job_id"):  # the run is on a worker: the answer goes back with its next heartbeat
            job = self.dispatcher.jobs.get(a["job_id"])
            if job is None or job.status != "claimed":
                raise KeyError(f"run {run_id} is not waiting for an approval at {node_id}")
            job.answers[node_id] = answer
            self.approvals.pop((run_id, node_id), None)
            self.dispatcher.save()
        elif not a["future"].done():
            a["future"].set_result(answer)
        return {"run_id": run_id, "node": node_id, "answer": "approve" if approve else "reject"}

    def pending_approvals(self) -> list[dict]:
        out = []
        for (rid, nid), a in self.approvals.items():
            run = self.runs.get(rid)
            out.append({"run_id": rid, "flow": run.flow if run else "", "node": nid, "question": a["question"],
                        "since": a["since"]})
        return out

    # ---- triggers ----
    def trigger_rows(self, name: str | None = None) -> list[dict]:
        """Every trigger of every saved flow, with its state (next run, last run)."""
        out = []
        for f in sorted(self.dir.glob("*.json")):
            if name and f.stem != name:
                continue
            try:
                doc = validate(json.loads(f.read_text(encoding="utf-8")), f.stem)
            except (OSError, ValueError):
                continue
            for t in doc.get("triggers") or []:
                st = self.trigger_state.d.get(f"{f.stem}/{t['id']}", {})
                row = {"flow": f.stem, **{k: v for k, v in t.items() if k != "inputs"}, "inputs": t.get("inputs", {}),
                       "next_run": trig_iso(st.get("next_run")) if t["type"] == "schedule" and t["enabled"] else None,
                       "last_run": st.get("last_run"), "last_run_id": st.get("last_run_id"),
                       "last_status": st.get("last_status"), "runs": st.get("runs", 0)}
                if t["type"] == "webhook":
                    row["path"] = f"/flows/v1/hooks/{f.stem}/{t['id']}"
                    row["secret_set"] = bool(os.getenv(t["secret_env"]))
                elif t["enabled"]:
                    row["warning"] = trig.check_schedule(t)
                out.append(row)
        return out

    async def _trigger_loop(self) -> None:
        while True:
            try:
                delay = self.tick()
            except Exception as e:  # a broken trigger never stops the loop
                self.bus.emit(Event("error", "flows", None, None, None, f"flow triggers: {type(e).__name__}: {e}"))
                delay = 30.0
            self._wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), max(0.05, min(delay, 30.0)))

    def tick(self, now: float | None = None) -> float:
        """Fire the schedule triggers that are due; return the seconds until the next one."""
        now = time.time() if now is None else now
        nearest = 30.0
        seen = set()
        if not trig.HAVE_SCHEDULES:
            return nearest
        for f in sorted(self.dir.glob("*.json")):
            try:
                doc = validate(json.loads(f.read_text(encoding="utf-8")), f.stem)
            except (OSError, ValueError):
                continue
            for t in doc.get("triggers") or []:
                if t["type"] != "schedule" or not t["enabled"]:
                    continue
                key = f"{f.stem}/{t['id']}"
                seen.add(key)
                st = self.trigger_state.get(key)
                fp = trig.fingerprint(t)
                try:
                    if st.get("fp") != fp or "next_run" not in st:
                        st.clear()
                        st.update(fp=fp, anchor=now, next_run=trig.next_time(t, now, now), runs=0)
                        self.trigger_state.save()
                    if st["next_run"] is None:
                        continue
                    if now >= st["next_run"]:
                        missed = now - st["next_run"] > trig.GRACE
                        st["next_run"] = trig.next_time(t, now, st.get("anchor"))
                        if not missed:
                            self._fire(f.stem, t, dict(t.get("inputs") or {}), st)
                        self.trigger_state.save()
                    if st.get("next_run"):
                        nearest = min(nearest, st["next_run"] - now)
                except (FlowError, ValueError):
                    continue
        for key in [k for k in self.trigger_state.d if k not in seen and not k.endswith(":hook")]:
            if self.trigger_state.d[key].get("fp"):  # a schedule trigger that was removed
                self.trigger_state.d.pop(key, None)
                self.trigger_state.save()
        return nearest

    def _fire(self, name: str, t: dict, inputs: dict, st: dict) -> Run | None:
        last = self.runs.get(st.get("last_run_id") or "")
        if t.get("overlap", "skip") == "skip" and last and last.status not in FINAL:
            st["last_status"] = "skipped (previous run still going)"
            self.bus.emit(Event("flow_run", "flows", None, None, None,
                                f"flow {name}: trigger {t['id']} skipped, its previous run is still going",
                                {"flow": name, "trigger": t["id"], "status": "skipped"}))
            return None
        try:
            run = self.start_run(name, inputs, by=f"{t['type']}:{t['id']}", trigger={"id": t["id"], "type": t["type"]})
        except (FlowError, KeyError, ValueError) as e:
            st.update(last_run=utcnow(), last_status=f"not started: {e.args[0] if e.args else e}")
            self.bus.emit(Event("error", "flows", None, None, None,
                                f"flow {name}: trigger {t['id']} could not start a run: {e.args[0] if e.args else e}"))
            return None
        st.update(last_run=utcnow(), last_run_id=run.id, last_status="started", runs=st.get("runs", 0) + 1)
        return run

    def hook(self, name: str, tid: str, body: bytes, headers: dict[str, str]) -> Run | None:
        """A webhook call (routes in __init__.py / workers.py). Raises KeyError (404), PermissionError (401),
        OverflowError (429) or FlowError (422)."""
        try:
            doc = validate(self.get(name), name)
        except (KeyError, FlowError):
            raise KeyError("no such webhook") from None
        t = next((x for x in doc.get("triggers") or [] if x["id"] == tid and x["type"] == "webhook"), None)
        if t is None or not t["enabled"]:
            raise KeyError("no such webhook")
        trig.verify(t, body, headers)
        if not self.ratelimit.allow(f"{name}/{tid}", int(t.get("max_per_minute", 30))):
            raise OverflowError("too many calls to this webhook; try again in a minute")
        st = self.trigger_state.get(f"{name}/{tid}:hook")
        run = self._fire(name, t, trig.hook_inputs(t, body, headers), st)
        self.trigger_state.save()
        if run is None and not str(st.get("last_status", "")).startswith("skipped"):
            raise FlowError(st.get("last_status") or "not started")
        return run  # None: skipped, the previous run is still going

    # ---- the run_flow tool ----
    def may_run(self, agent) -> bool:
        return self.agents_may_run == "*" or getattr(agent, "name", "") in self.agents_may_run

    async def tool_run(self, task, agent, inp: dict) -> str:
        if task is not None and (task.root_id in self.steps.task_ids or task.id in self.steps.task_ids):
            return "run_flow is not available inside a step of a flow (flows do not start other flows)"
        name = str(inp.get("name") or "")
        try:
            run = self.start_run(name, inp.get("inputs") or {}, by=getattr(agent, "name", "agent"))
        except (FlowError, KeyError, ValueError) as e:
            return f"could not start flow {name!r}: {e.args[0] if e.args else e}"
        run = await self.wait(run.id, self.tool_timeout)
        if run.status not in FINAL:
            return f"flow {name} is still running after {self.tool_timeout:.0f}s (run {run.id}); see the Flows page"
        if run.status != "done":
            return f"flow {name} ended with {run.status}: {run.error}"
        return run.result or f"flow {name} finished without output"

    def describe_for_agent(self) -> str:
        rows = [f"- {f['name']}: {f['title']}" + (f" (inputs: {', '.join(f['inputs'])})" if f.get("inputs") else "")
                for f in self.list() if not f.get("error")]
        return ("Saved flows you can start with run_flow (it waits for the result):\n" + "\n".join(rows)) if rows else ""

    def status(self) -> dict:
        return {"flows": len(list(self.dir.glob("*.json"))),
                "running": len(self.running()), "waiting_approval": len(self.approvals),
                "workers": len(self.dispatcher.workers),
                "listen": self.dispatcher.status()["listen"]}
