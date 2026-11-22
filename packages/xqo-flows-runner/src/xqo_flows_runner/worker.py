"""The headless worker: polls an HQ's flows worker API for jobs and runs them with this machine's own agents.

No dashboard, no office, no listener: the worker only makes outgoing HTTP calls to the HQ (workers.py in
xqo-flows), so it can run behind NAT. Its agents, API keys, sandbox and budgets come from its own agents.json.

Jobs
- "task": one agent step of a flow that runs on the HQ ({agent, prompt, title, unattended}). The step becomes an
  ordinary root task here; its result goes back to the HQ.
- "flow": a whole flow ({flow, inputs, run_id}), run here by the same engine as on the HQ. Node states go back with
  every heartbeat, so the HQ's editor shows the run live.

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
        self.log(f"job {jid}: {job['kind']} {job['spec'].get('title', '')}")
        work = asyncio.ensure_future(self._work(job, progress))
        beat = asyncio.ensure_future(self._heartbeat(jid, work, progress))
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

    async def _heartbeat(self, jid: str, work: asyncio.Task, progress: dict) -> None:
        while not work.done():
            await asyncio.sleep(max(0.05, self.lease / 3))
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

    async def _work(self, job: dict, progress: dict) -> dict:
        spec = job.get("spec") or {}
        if job["kind"] == "task":
            def created(task):
                progress["task_id"] = task.id
            return await run_task(self.bus, str(spec.get("prompt") or ""), spec.get("agent") or None,
                                  str(spec.get("title") or "flow step"), unattended=spec.get("unattended", True) is not False,
                                  on_created=created)
        if job["kind"] == "flow":
            doc = validate(spec.get("flow"), (spec.get("flow") or {}).get("name"))
            run = Run(flow=doc["name"], title=doc.get("title", ""), inputs=spec.get("inputs") or {},
                      id=spec.get("run_id") or job["id"], by="hq", where="worker-local")

            def changed(r, nid):
                progress["nodes"] = r.nodes

            steps = BusSteps(self.bus, changed)
            steps.where = "a worker (approval and remote steps run on the HQ)"
            run = await Engine(steps, changed).run(doc, run)
            return {"status": run.status if run.status in ("done", "error", "cancelled") else "error",
                    "result": run.result, "error": run.error}
        raise FlowError(f"unknown job kind {job['kind']!r}")
