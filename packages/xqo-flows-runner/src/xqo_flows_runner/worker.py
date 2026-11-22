"""The headless worker: polls an HQ's flows worker API for jobs and runs them with this machine's own agents.

No dashboard, no office, no listener: the worker only makes outgoing HTTP calls to the HQ (workers.py in
xqo-flows), so it can run behind NAT. Its agents, API keys, sandbox and budgets come from its own agents.json.

Jobs
- "task": one agent step of a flow that runs on the HQ ({agent, prompt, title, unattended}). The step becomes an
  ordinary root task here; its result goes back to the HQ.
- "flow": a whole flow ({flow, inputs, run_id, approval_timeout, nodes}), run here by the same engine as on the HQ.
  Node states go back with every heartbeat, so the HQ's editor shows the run live. An approval step is asked on
  the HQ: the question goes up with the heartbeats (progress["approvals"]) and the HQ's answer comes back in the
  heartbeat's reply ({"answers": {node: "approve"|"reject"}}). "nodes" is set when the HQ resumes a run: steps
  already done keep their results.

While a job runs the worker sends a heartbeat every lease/3 seconds; when the HQ answers {"cancel": true} the job's
task is cancelled here. If the HQ cannot be reached the worker keeps trying with a growing pause (up to 30 s)."""
from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import urllib.error
import urllib.request

from xqo.api import __version__

from xqo_flows import BusSteps, Engine, FlowError, Run, run_task, validate


class HQError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(f"{status}: {detail}")
        self.status = status


class HQClient:
    """The worker API of one HQ (blocking calls, run in a thread)."""

    def __init__(self, url: str, name: str, token: str, timeout: float = 60):
        self.url, self.name, self.token, self.timeout = url.rstrip("/"), name, token, timeout

    def call(self, method: str, path: str, body: dict | None = None, timeout: float | None = None) -> dict:
        req = urllib.request.Request(self.url + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", "X-XQO-Worker": self.name,
                                              "Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read()).get("detail")
            except ValueError:
                detail = e.reason
            raise HQError(e.code, str(detail)) from None

    async def acall(self, method: str, path: str, body: dict | None = None, timeout: float | None = None) -> dict:
        return await asyncio.to_thread(self.call, method, path, body, timeout)


class WorkerSteps(BusSteps):
    """Agent steps run here; approval steps are asked on the HQ through the heartbeats."""

    where = "a worker (remote steps run on the HQ)"

    def __init__(self, bus, changed, progress: dict, poke: asyncio.Event, approval_timeout: float | None):
        super().__init__(bus, changed)
        self.progress = progress
        self.poke = poke
        self.approval_timeout = approval_timeout
        self.waiting: dict[str, asyncio.Future] = {}

    async def approval(self, run: Run, node: dict, question: str) -> str:
        nid = node["id"]
        fut = asyncio.get_running_loop().create_future()
        self.waiting[nid] = fut
        self.progress.setdefault("approvals", {})[nid] = {"question": question}
        run.nodes[nid]["question"] = question
        self._changed(run, nid)
        self.poke.set()  # tell the HQ now, not at the next regular heartbeat
        timeout = node["data"].get("timeout") or self.approval_timeout
        try:
            answer, by = await asyncio.wait_for(asyncio.shield(fut), timeout), "user"
        except asyncio.TimeoutError:
            answer, by = node["data"].get("default", "reject"), "timeout"
        finally:
            self.waiting.pop(nid, None)
            self.progress.get("approvals", {}).pop(nid, None)
            self.poke.set()
        run.nodes[nid].update(answer=answer, answered_by=by)
        return answer

    def answers(self, answers: dict) -> None:
        for nid, a in (answers or {}).items():
            fut = self.waiting.get(nid)
            if fut and not fut.done() and a in ("approve", "reject"):
                fut.set_result(a)


class Worker:
    def __init__(self, bus, hq: HQClient, jobs: int = 1, poll_wait: float = 20, log=None):
        self.bus, self.hq = bus, hq
        self.slots = max(1, int(jobs))
        self.poll_wait = poll_wait
        self.lease = 60.0
        self.active: dict[str, asyncio.Task] = {}
        self.done = 0
        self.log = log or (lambda msg: print(msg, file=sys.stderr, flush=True))
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def ping(self) -> dict:
        info = await self.hq.acall("GET", "/flows/v1/ping")
        self.lease = float(info.get("lease_seconds", self.lease))
        return info

    async def run(self, max_jobs: int | None = None) -> None:
        """Poll and run jobs until stop() (or until max_jobs jobs have finished)."""
        pause = 1.0
        await self.bus.mcp.start()
        try:
            while not self._stop.is_set():
                if max_jobs is not None and self.done + len(self.active) >= max_jobs:
                    if not self.active:
                        break
                    await asyncio.wait(set(self.active.values()), return_when=asyncio.FIRST_COMPLETED)
                    continue
                if len(self.active) >= self.slots:
                    await asyncio.wait(set(self.active.values()), return_when=asyncio.FIRST_COMPLETED)
                    continue
                try:
                    r = await self.hq.acall("POST", "/flows/v1/next", {"wait": self.poll_wait, "version": __version__},
                                            timeout=self.poll_wait + 30)
                    pause = 1.0
                except HQError as e:
                    if e.status in (401, 403):
                        self.log(f"the HQ refused this worker: {e}")
                        raise
                    self.log(f"HQ error: {e}; retrying in {pause:.0f}s")
                    await self._sleep(pause)
                    pause = min(pause * 2, 30)
                    continue
                except (urllib.error.URLError, OSError, ValueError) as e:
                    self.log(f"HQ not reachable ({e}); retrying in {pause:.0f}s")
                    await self._sleep(pause)
                    pause = min(pause * 2, 30)
                    continue
                self.lease = float(r.get("lease_seconds", self.lease))
                job = r.get("job")
                if job:
                    self.active[job["id"]] = asyncio.ensure_future(self.handle(job))
        finally:
            for t in list(self.active.values()):
                t.cancel()
            if self.active:
                await asyncio.wait(set(self.active.values()), timeout=60)
            await self.bus.mcp.stop()

    async def _sleep(self, s: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), s)

    async def handle(self, job: dict) -> None:
        jid = job["id"]
        progress: dict = {}
        poke = asyncio.Event()
        box: dict = {}  # the job's WorkerSteps, for answers that come back with heartbeats
        self.log(f"job {jid}: {job['kind']} {job['spec'].get('title', '')}")
        work = asyncio.ensure_future(self._work(job, progress, poke, box))
        beat = asyncio.ensure_future(self._heartbeat(jid, work, progress, poke, box))
        status, result, error = "error", "", ""
        try:
            out = await work
            status, result, error = out.get("status", "done"), out.get("result", ""), out.get("error", "")
        except asyncio.CancelledError:
            status, error = "cancelled", "cancelled on the worker"
        except FlowError as e:
            error = str(e)
        except Exception as e:  # a job never stops the worker
            error = f"{type(e).__name__}: {e}"
        finally:
            beat.cancel()
            with contextlib.suppress(BaseException):
                await beat
            body = {"status": status, "result": result, "error": error, "task_id": progress.get("task_id", ""),
                    **({"progress": progress} if progress.get("nodes") else {})}
            for attempt in range(5):
                try:
                    await self.hq.acall("POST", f"/flows/v1/jobs/{jid}/result", body)
                    break
                except HQError as e:
                    self.log(f"job {jid}: the HQ did not take the result: {e}")
                    break
                except (urllib.error.URLError, OSError) as e:
                    self.log(f"job {jid}: sending the result failed ({e}); retry {attempt + 1}/5")
                    await asyncio.sleep(min(2 ** attempt, 15))
            self.active.pop(jid, None)
            self.done += 1
            self.log(f"job {jid}: {status}" + (f" ({error[:200]})" if error else ""))

    async def _heartbeat(self, jid: str, work: asyncio.Task, progress: dict, poke: asyncio.Event | None = None,
                         box: dict | None = None) -> None:
        poke = poke or asyncio.Event()
        box = box if box is not None else {}
        while not work.done():
            # every lease/3; every second while an approval waits (so its answer comes back quickly)
            pause = 1.0 if progress.get("approvals") else self.lease / 3
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(poke.wait(), max(0.05, pause))
            poke.clear()
            if work.done():
                return
            try:
                r = await self.hq.acall("POST", f"/flows/v1/jobs/{jid}/heartbeat", {"progress": progress})
            except HQError as e:
                if e.status == 404:
                    work.cancel()
                    return
                continue
            except (urllib.error.URLError, OSError, ValueError):
                continue  # the lease decides; a short outage is fine
            if r.get("cancel"):
                self.log(f"job {jid}: cancelled by the HQ")
                work.cancel()
                return
            if r.get("answers") and box.get("steps"):
                box["steps"].answers(r["answers"])

    async def _work(self, job: dict, progress: dict, poke: asyncio.Event | None = None, box: dict | None = None) -> dict:
        spec = job.get("spec") or {}
        if job["kind"] == "task":
            def created(task):
                progress["task_id"] = task.id
            return await run_task(self.bus, str(spec.get("prompt") or ""), spec.get("agent") or None,
                                  str(spec.get("title") or "flow step"), unattended=spec.get("unattended", True) is not False,
                                  on_created=created)
        if job["kind"] == "flow":
            doc = validate(spec.get("flow"), (spec.get("flow") or {}).get("name"))
            resume = isinstance(spec.get("nodes"), dict) and bool(spec["nodes"])
            run = Run(flow=doc["name"], title=doc.get("title", ""), inputs=spec.get("inputs") or {},
                      id=spec.get("run_id") or job["id"], by="hq", where="worker-local",
                      nodes=json.loads(json.dumps(spec["nodes"])) if resume else {})

            def changed(r, nid):
                progress["nodes"] = r.nodes

            try:
                timeout = float(spec.get("approval_timeout") or 0) or None
            except (TypeError, ValueError):
                timeout = None
            steps = WorkerSteps(self.bus, changed, progress, poke or asyncio.Event(), timeout)
            if box is not None:
                box["steps"] = steps
            run = await Engine(steps, changed).run(doc, run, resume=resume)
            return {"status": run.status if run.status in ("done", "error", "cancelled") else "error",
                    "result": run.result, "error": run.error}
        raise FlowError(f"unknown job kind {job['kind']!r}")
