"""The flow runner: walks a validated flow (model.py) from its start node and runs each step.

Execution rules
- A node starts when every incoming edge is decided. It runs if at least one of them is alive, and is skipped
  (with all its outgoing edges dead) if none is: so a join after a condition waits only for the branch taken.
- Edges leaving a node run their targets in parallel. A condition makes the edges of its chosen port alive and
  the others dead; an approval does the same with its "approve" / "reject" ports.
- A failed step fails the run and cancels the steps still going, unless the node has ``continue_on_error``.
- The run's result is the rendered output of the end node(s) reached, or the last step's result without one.

The steps themselves (an xqo task, a remote job, waiting for a person) are done by a ``Steps`` object, so the
same engine runs on the HQ (service.py) and on a headless worker (xqo-flows-runner)."""
from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from xqo.api import new_id, utcnow

from .model import FlowError, check, ports, render

FINAL = {"done", "error", "cancelled", "interrupted"}


@dataclass
class Run:
    flow: str
    title: str = ""
    inputs: dict = field(default_factory=dict)
    id: str = field(default_factory=lambda: new_id("frun"))
    status: str = "running"  # running | waiting | done | error | cancelled | interrupted
    started: str = field(default_factory=utcnow)
    ended: str | None = None
    result: str = ""
    error: str = ""
    by: str = "user"
    where: str = "hq"  # hq | worker:<name> (a whole run sent to a worker) | worker-local
    nodes: dict = field(default_factory=dict)  # node id -> {status, title, type, started, ended, output, ...}
    job_id: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Run":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class Steps:
    """What a node does. Subclasses implement the kinds they support; the rest fail with a clear message."""

    where = "this machine"

    async def agent(self, run: Run, node: dict, prompt: str) -> dict:
        raise FlowError(f"agent steps cannot run on {self.where}")

    async def remote(self, run: Run, node: dict, prompt: str) -> dict:
        raise FlowError(f"remote steps cannot run on {self.where}")

    async def approval(self, run: Run, node: dict, question: str) -> str:
        raise FlowError(f"approval steps cannot run on {self.where}")


class Engine:
    def __init__(self, steps: Steps, changed: Callable[[Run, str | None], None] = lambda run, node: None):
        self.steps = steps
        self.changed = changed  # called after every state change: persist and emit events

    def _set(self, run: Run, nid: str, **kw) -> None:
        run.nodes[nid].update(kw)
        self.changed(run, nid)

    async def run(self, doc: dict, run: Run) -> Run:
        nodes = {n["id"]: n for n in doc["nodes"]}
        out_edges: dict[str, list[int]] = {n: [] for n in nodes}
        in_edges: dict[str, list[int]] = {n: [] for n in nodes}
        for i, e in enumerate(doc["edges"]):
            out_edges[e["sourceNodeID"]].append(i)
            in_edges[e["targetNodeID"]].append(i)
        alive: dict[int, bool] = {}
        for nid, n in nodes.items():
            run.nodes.setdefault(nid, {"status": "pending", "title": n["data"].get("title", nid), "type": n["type"]})
        defaults = {i["name"]: i.get("default", "") for i in next(n for n in nodes.values()
                                                                  if n["type"] == "start")["data"].get("inputs", [])}
        inputs = {**defaults, **{k: ("" if v is None else str(v)) for k, v in (run.inputs or {}).items()}}
        run.inputs = inputs
        scope: dict[str, Any] = {"inputs": inputs}
        ends: list[str] = []
        last: list[str] = []
        jobs: dict[asyncio.Task, str] = {}
        failure: list[str] = []

        def decide(nid: str, port: str | None) -> list[str]:
            """Mark the edges leaving nid alive/dead; return the targets that became ready."""
            ready = []
            for i in out_edges[nid]:
                e = doc["edges"][i]
                alive[i] = port is None or e.get("sourcePortID") == port
                t = e["targetNodeID"]
                if all(j in alive for j in in_edges[t]) and run.nodes[t]["status"] == "pending":
                    ready.append(t)
            return ready

        def skip(nid: str) -> list[str]:
            self._set(run, nid, status="skipped")
            ready = []
            for i in out_edges[nid]:
                alive[i] = False
                t = doc["edges"][i]["targetNodeID"]
                if all(j in alive for j in in_edges[t]) and run.nodes[t]["status"] == "pending":
                    ready.append(t)
            return ready

        def launch(nid: str) -> None:
            if not any(alive.get(i) for i in in_edges[nid]):
                for t in skip(nid):
                    launch(t)
                return
            self._set(run, nid, status="running", started=utcnow())
            jobs[asyncio.ensure_future(self._step(run, nodes[nid], scope))] = nid

        start = next(n for n in nodes.values() if n["type"] == "start")
        scope[start["id"]] = {"result": "", "status": "done", **inputs}
        self._set(run, start["id"], status="done", started=run.started, ended=utcnow(), output={"inputs": inputs})
        for t in decide(start["id"], None):
            launch(t)
        try:
            while jobs:
                done, _ = await asyncio.wait(set(jobs), return_when=asyncio.FIRST_COMPLETED)
                for fut in done:
                    nid = jobs.pop(fut)
                    node = nodes[nid]
                    try:
                        output, port = fut.result()
                        status = output.get("status", "done")
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        msg = str(e) if isinstance(e, FlowError) else f"{type(e).__name__}: {e}"
                        output, port, status = {"result": "", "status": "error", "error": msg}, None, "error"
                    scope[nid] = output
                    ok = status == "done" or node["data"].get("continue_on_error")
                    self._set(run, nid, status="done" if status == "done" else "error", ended=utcnow(),
                              output=output, **({"port": port} if port else {}),
                              **({"error": output.get("error") or output.get("result", "")[:2000]}
                                 if status != "done" else {}))
                    if not ok:
                        failure.append(f"{node['data'].get('title', nid)}: "
                                       f"{output.get('error') or output.get('result') or status}"[:2000])
                        continue
                    if node["type"] == "end":
                        ends.append(nid)
                    elif node["type"] in ("agent", "remote"):
                        last.append(nid)
                    for t in decide(nid, port):
                        launch(t)
                if failure:
                    break
            if failure:
                await self._cancel_all(jobs, run)
            for nid, st in run.nodes.items():  # never reached: behind a failed step
                if st["status"] == "pending":
                    st["status"] = "skipped"
            if failure:
                run.status, run.error = "error", failure[0]
            else:
                run.status = "done"
                outs = [scope[n].get("result", "") for n in ends] or ([scope[last[-1]].get("result", "")] if last else [])
                run.result = "\n\n".join(o for o in outs if o)
        except asyncio.CancelledError:
            await self._cancel_all(jobs, run)
            run.status = "cancelled"
            run.error = run.error or "cancelled"
            raise
        finally:
            run.ended = utcnow()
            self.changed(run, None)
        return run

    async def _cancel_all(self, jobs: dict, run: Run) -> None:
        for fut, nid in jobs.items():
            fut.cancel()
        if jobs:
            await asyncio.wait(set(jobs), timeout=30)
        for nid in jobs.values():
            if run.nodes[nid]["status"] in ("running", "waiting"):
                run.nodes[nid].update(status="cancelled", ended=utcnow())
        jobs.clear()

    async def _step(self, run: Run, node: dict, scope: dict) -> tuple[dict, str | None]:
        t, d = node["type"], node["data"]
        if t == "agent":
            return await self.steps.agent(run, node, render(d["prompt"], scope)), None
        if t == "remote":
            return await self.steps.remote(run, node, render(d["prompt"], scope)), None
        if t == "condition":
            for c in d["conditions"]:
                if check(c, scope):
                    return {"result": c["key"], "status": "done"}, c["key"]
            return {"result": "else", "status": "done"}, "else"
        if t == "approval":
            self._set(run, node["id"], status="waiting")
            answer = await self.steps.approval(run, node, render(d["question"], scope))
            if answer not in ports(node):
                raise FlowError(f"approval answer must be approve or reject, got {answer!r}")
            self._set(run, node["id"], status="running")
            return {"result": answer, "status": "done"}, answer
        if t == "end":
            return {"result": render(d.get("output", ""), scope), "status": "done"}, None
        raise FlowError(f"unknown node type {t}")


async def wait_for(fut: asyncio.Future, timeout: float | None):
    """asyncio.wait_for that leaves `fut` running when it times out (the caller decides what to do)."""
    if timeout is None:
        return await asyncio.shield(fut)
    t0 = time.monotonic()
    done, _ = await asyncio.wait({fut}, timeout=timeout)
    if not done:
        raise asyncio.TimeoutError(f"no answer after {time.monotonic() - t0:.0f}s")
    return fut.result()
