"""Agent steps on an xqo bus: one ordinary root task per step (it shows in the dashboard, the office and the
task list like any other). Used by the HQ (service.py) and by headless workers (xqo-flows-runner)."""
from __future__ import annotations

import asyncio
import contextlib

from .engine import Run, Steps
from .model import FlowError


async def run_task(bus, prompt: str, agent: str | None, title: str, unattended: bool = True,
                   on_created=None) -> dict:
    """Run one root task to the end and return {result, status, task_id}. Cancelling the caller cancels the
    task (through bus.cancel, so its workspace and log are settled like a cancel from the dashboard)."""
    name = None
    if agent:
        name = bus.agent_id(agent)
        if not name:
            raise FlowError(f"unknown agent {agent!r}")
    task = await bus.create(prompt, name, title=title[:120], meta={"unattended": unattended})
    if on_created:
        on_created(task)
    job = bus.spawn(bus._run(task))  # the same path as a scheduled run (xqo-schedules)
    try:
        await asyncio.shield(job)
    except asyncio.CancelledError:
        with contextlib.suppress(KeyError, ValueError):
            await bus.cancel(task.id, "flows", "the flow run was cancelled", wait=0)
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(asyncio.shield(job), 30)
        raise
    return {"result": task.result or "", "status": "done" if task.status == "done" else "error",
            "task_status": task.status, "task_id": task.id}


class BusSteps(Steps):
    where = "this machine"

    def __init__(self, bus, changed=None):
        self.bus = bus
        self.task_ids: set[str] = set()  # root tasks started by flow steps (run_flow refuses to nest in them)
        self._changed = changed or (lambda run, nid: None)

    async def agent(self, run: Run, node: dict, prompt: str) -> dict:
        d = node["data"]

        def created(task):
            self.task_ids.add(task.id)
            run.nodes[node["id"]]["task_id"] = task.id
            self._changed(run, node["id"])

        return await run_task(self.bus, prompt, d.get("agent") or None, f"{run.flow} · {d.get('title') or node['id']}",
                              unattended=d.get("unattended", True) is not False, on_created=created)
