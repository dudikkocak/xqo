"""Remote workers: a job queue on the HQ and the small HTTP API that headless workers (xqo-flows-runner) poll.

Workers connect out to the HQ (nothing listens on the worker), so they work behind NAT and firewalls:

    POST /flows/v1/next                  long poll: the next job for this worker, or {"job": null}
    POST /flows/v1/jobs/{id}/heartbeat   keeps the job's lease; carries progress; answers {"cancel": bool,
                                         "answers": {approval node: "approve"|"reject"}}
    POST /flows/v1/jobs/{id}/result      {"status": "done"|"error"|"cancelled", "result", "error", "task_id"}
    GET  /flows/v1/ping                  who am I, which labels
    POST /flows/v1/hooks/{flow}/{trigger} a webhook trigger (no worker token: the trigger's own secret, triggers.py)

Every request carries ``X-XQO-Worker: <name>`` and ``Authorization: Bearer <token>``. Workers are listed on the
HQ, each with its own token (from an environment variable) and its labels; the HQ decides which labels a worker
has, so a worker cannot take jobs meant for others. The API is served on its own listener
("flows.workers.listen"), so the dashboard stays on localhost.

    "flows": {"workers": {"listen": "0.0.0.0:8200", "lease_seconds": 60, "max_attempts": 2,
                          "allow": {"gpu-box": {"token_env": "XQO_WORKER_GPU_BOX_TOKEN", "labels": ["gpu", "linux"]}}}}

Jobs: "task" (one agent step of a flow that runs on the HQ) or "flow" (a whole flow, run by the worker's own
engine and agents). A claimed job whose worker stops sending heartbeats for ``lease_seconds`` goes back to the
queue (up to ``max_attempts`` claims), then fails. Jobs still open when the HQ restarts are cancelled: the runs
waiting for them were interrupted by the restart."""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request

from xqo.api import new_id, utcnow

from .model import FlowError

MAX_BODY = 4 * 1024 * 1024
MAX_WAIT = 30.0
KEEP_FINISHED = 200


def parse_listen(value: str | None) -> tuple[str, int] | None:
    """"0.0.0.0:8200" / ":8200" / "8200" -> (host, port); None or "" = no listener."""
    if not value:
        return None
    host, _, port = str(value).rpartition(":")
    try:
        return host or "0.0.0.0", int(port)
    except ValueError:
        raise FlowError(f"flows.workers.listen must be host:port, got {value!r}") from None


@dataclass
class WorkerCfg:
    name: str
    token_env: str
    labels: list[str]

    def token(self) -> str:
        return os.getenv(self.token_env, "")


@dataclass
class Job:
    kind: str  # task | flow
    spec: dict
    labels: list[str] = field(default_factory=list)
    worker: str = ""  # only this worker may take it ("" = any with the labels)
    id: str = field(default_factory=lambda: new_id("fjob"))
    status: str = "queued"  # queued | claimed | done | error | cancelled
    created: str = field(default_factory=utcnow)
    claimed_by: str = ""
    claimed_at: str = ""
    attempts: int = 0
    lease_until: float = 0.0  # time.time()
    cancel: bool = False
    result: str = ""
    error: str = ""
    task_id: str = ""  # the task on the worker
    run_id: str = ""
    node_id: str = ""
    ended: str = ""
    answers: dict = field(default_factory=dict)  # approval node id -> "approve" | "reject" (whole-flow jobs)

    def public(self) -> dict:
        d = asdict(self)
        d.pop("spec")
        d["title"] = self.spec.get("title", "")
        return d

    def for_worker(self) -> dict:
        return {"id": self.id, "kind": self.kind, "spec": self.spec, "attempt": self.attempts,
                "run_id": self.run_id, "node_id": self.node_id}


class Dispatcher:
    def __init__(self, cfg: dict | None, data_dir: Path, hq: str = "hq"):
        cfg = cfg or {}
        unknown = set(cfg) - {"listen", "lease_seconds", "max_attempts", "allow"}
        if unknown:
            raise FlowError(f"flows.workers: unknown key(s) {', '.join(sorted(unknown))}")
        self.hq = hq
        self.listen = parse_listen(os.getenv("XQO_FLOWS_LISTEN", cfg.get("listen") or ""))
        self.lease = float(cfg.get("lease_seconds", 60))
        self.max_attempts = int(cfg.get("max_attempts", 2))
        self.workers: dict[str, WorkerCfg] = {}
        for name, w in (cfg.get("allow") or {}).items():
            if not isinstance(w, dict):
                raise FlowError(f"flows.workers.allow.{name} must be an object")
            if w.get("token"):
                raise FlowError(f"flows.workers.allow.{name} has a plaintext token; put it in an environment "
                                "variable and name that with token_env")
            labels = w.get("labels") or []
            if not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
                raise FlowError(f"flows.workers.allow.{name}.labels must be a list of text")
            env = w.get("token_env") or f"XQO_WORKER_{name.upper().replace('-', '_')}_TOKEN"
            self.workers[name] = WorkerCfg(name, env, labels)
        self.path = data_dir / "jobs.json"
        self.jobs: dict[str, Job] = {}
        self.futures: dict[str, asyncio.Future] = {}
        self.seen: dict[str, dict] = {}
        self._wake: asyncio.Event | None = None
        self._loop_task: asyncio.Task | None = None
        self._load()

    # ---- state ----
    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            rows = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for d in rows:
            job = Job(**{k: v for k, v in d.items() if k in Job.__dataclass_fields__})
            if job.status in ("queued", "claimed"):  # the run waiting for it did not survive the restart
                job.status, job.cancel, job.error, job.ended = "cancelled", True, "the HQ restarted", utcnow()
            self.jobs[job.id] = job
        self.save()

    def save(self) -> None:
        done = [j for j in self.jobs.values() if j.status not in ("queued", "claimed")]
        for j in done[:-KEEP_FINISHED]:
            self.jobs.pop(j.id, None)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(j) for j in self.jobs.values()], indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def _event(self) -> asyncio.Event:
        if self._wake is None:
            self._wake = asyncio.Event()
        return self._wake

    def _notify(self) -> None:
        ev = self._event()
        ev.set()
        self._wake = asyncio.Event()

    # ---- the HQ side ----
    def matching(self, labels: list[str], worker: str = "") -> list[str]:
        return [w.name for w in self.workers.values()
                if (not worker or w.name == worker) and set(labels) <= set(w.labels)]

    def submit(self, kind: str, spec: dict, labels: list[str] | None = None, worker: str = "",
               run_id: str = "", node_id: str = "") -> Job:
        labels = list(labels or [])
        if worker and worker not in self.workers:
            raise FlowError(f"no worker named {worker!r} is listed in flows.workers.allow")
        if not self.matching(labels, worker):
            raise FlowError("no worker listed in flows.workers.allow has the labels " + (", ".join(labels) or "(any)")
                            + (f" (worker {worker})" if worker else ""))
        job = Job(kind, spec, labels, worker, run_id=run_id, node_id=node_id)
        self.jobs[job.id] = job
        self.futures[job.id] = asyncio.get_running_loop().create_future()
        self.save()
        self._notify()
        return job

    def future(self, job_id: str) -> asyncio.Future:
        return self.futures[job_id]

    def cancel(self, job_id: str, reason: str = "cancelled") -> None:
        job = self.jobs.get(job_id)
        if not job or job.status not in ("queued", "claimed"):
            return
        job.cancel = True
        job.error = reason
        if job.status == "queued":
            self._finish(job, "cancelled", "", reason)
        else:
            self.save()  # the worker sees it on its next heartbeat and stops the task

    def _finish(self, job: Job, status: str, result: str, error: str, task_id: str = "") -> None:
        job.status, job.result, job.error, job.ended = status, result, error, utcnow()
        job.task_id = task_id or job.task_id
        self.save()
        fut = self.futures.pop(job.id, None)
        if fut and not fut.done():
            fut.set_result({"status": status, "result": result, "error": error, "worker": job.claimed_by,
                            "task_id": job.task_id})

    def progress_hook(self, fn) -> None:
        """fn(job, progress dict) is called for every heartbeat that carries progress (service.py)."""
        self._progress = fn

    _progress = staticmethod(lambda job, progress: None)

    async def start(self) -> None:
        self._loop_task = asyncio.ensure_future(self._monitor())

    async def stop(self) -> None:
        if self._loop_task:
            self._loop_task.cancel()
            with contextlib.suppress(BaseException):
                await self._loop_task

    async def _monitor(self) -> None:
        while True:
            await asyncio.sleep(min(5.0, max(0.2, self.lease / 4)))
            self.expire()

    def expire(self) -> None:
        now = time.time()
        for job in list(self.jobs.values()):
            if job.status == "claimed" and job.lease_until < now:
                if job.cancel:
                    self._finish(job, "cancelled", "", job.error or "cancelled")
                elif job.attempts >= self.max_attempts:
                    self._finish(job, "error", "", f"worker {job.claimed_by} stopped answering "
                                                   f"({job.attempts} attempt{'s' if job.attempts != 1 else ''})")
                else:
                    job.status, job.claimed_by = "queued", ""
                    self.save()
                    self._notify()

    def status(self) -> dict:
        now = time.time()
        return {"listen": f"{self.listen[0]}:{self.listen[1]}" if self.listen else None,
                "workers": [{"name": w.name, "labels": w.labels, "token_env": w.token_env, "token_set": bool(w.token()),
                             "last_seen": self.seen.get(w.name, {}).get("ts"),
                             "online": now - self.seen.get(w.name, {}).get("t", 0) < MAX_WAIT + 15,
                             "version": self.seen.get(w.name, {}).get("version", ""),
                             "running": [j.id for j in self.jobs.values()
                                         if j.status == "claimed" and j.claimed_by == w.name]}
                            for w in self.workers.values()],
                "queued": sum(1 for j in self.jobs.values() if j.status == "queued"),
                "jobs": [j.public() for j in sorted(self.jobs.values(), key=lambda j: j.created, reverse=True)[:50]]}

    # ---- the worker side (called by the API below) ----
    def authenticate(self, name: str, token: str) -> WorkerCfg:
        w = self.workers.get(name or "")
        if w is None:
            raise HTTPException(401, "unknown worker")
        expected = w.token()
        if not expected:
            raise HTTPException(403, f"the HQ has no token for worker {name} (set {w.token_env})")
        if not hmac.compare_digest(expected.encode(), (token or "").encode()):
            raise HTTPException(401, "wrong token")
        return w

    def _touch(self, w: WorkerCfg, version: str = "") -> None:
        self.seen[w.name] = {"t": time.time(), "ts": utcnow(), "version": version or self.seen.get(w.name, {}).get("version", "")}

    def _take(self, w: WorkerCfg) -> Job | None:
        for job in sorted(self.jobs.values(), key=lambda j: j.created):
            if job.status == "queued" and not job.cancel and (not job.worker or job.worker == w.name) \
                    and set(job.labels) <= set(w.labels):
                job.status, job.claimed_by, job.claimed_at = "claimed", w.name, utcnow()
                job.attempts += 1
                job.lease_until = time.time() + self.lease
                self.save()
                return job
        return None

    async def next_job(self, w: WorkerCfg, wait: float, version: str = "") -> Job | None:
        self._touch(w, version)
        deadline = time.monotonic() + max(0.0, min(wait, MAX_WAIT))
        while True:
            job = self._take(w)
            if job or time.monotonic() >= deadline:
                self._touch(w)
                return job
            ev = self._event()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(ev.wait(), deadline - time.monotonic())

    def _claimed(self, w: WorkerCfg, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None or job.claimed_by != w.name:
            raise HTTPException(404, "no such job for this worker")
        return job

    def heartbeat(self, w: WorkerCfg, job_id: str, progress: dict | None) -> dict:
        self._touch(w)
        job = self._claimed(w, job_id)
        if job.status != "claimed":
            return {"cancel": True, "status": job.status}
        job.lease_until = time.time() + self.lease
        if progress:
            if progress.get("task_id"):
                job.task_id = str(progress["task_id"])[:100]
            try:
                self._progress(job, progress)
            except Exception:  # progress is informational; a bad report never fails the job
                pass
        return {"cancel": job.cancel, "answers": job.answers}

    def result(self, w: WorkerCfg, job_id: str, body: dict) -> dict:
        self._touch(w)
        job = self._claimed(w, job_id)
        if job.status != "claimed":
            return {"ok": False, "status": job.status}
        status = body.get("status")
        if status not in ("done", "error", "cancelled"):
            raise HTTPException(422, "status must be done, error or cancelled")
        if body.get("progress"):
            with contextlib.suppress(Exception):
                self._progress(job, body["progress"])
        self._finish(job, status, str(body.get("result") or "")[:1_000_000], str(body.get("error") or "")[:10_000],
                     str(body.get("task_id") or "")[:100])
        return {"ok": True}


def create_worker_app(disp: Dispatcher, service=None) -> FastAPI:
    """The public listener: the worker API, plus webhook triggers when `service` (a FlowService) is given."""
    app = FastAPI(title="xqo flows worker API", docs_url=None, redoc_url=None, openapi_url=None)

    def auth(name: str | None, authorization: str | None) -> WorkerCfg:
        return disp.authenticate(name or "", (authorization or "").removeprefix("Bearer ").strip())

    async def body(request: Request) -> dict:
        raw = await request.body()
        if len(raw) > MAX_BODY:
            raise HTTPException(413, "body too large")
        if not raw:
            return {}
        try:
            d = json.loads(raw)
        except ValueError:
            raise HTTPException(422, "body must be JSON")
        if not isinstance(d, dict):
            raise HTTPException(422, "body must be a JSON object")
        return d

    @app.get("/flows/v1/ping")
    async def ping(x_xqo_worker: str | None = Header(default=None), authorization: str | None = Header(default=None)):
        w = auth(x_xqo_worker, authorization)
        disp._touch(w)
        return {"hq": disp.hq, "worker": w.name, "labels": w.labels, "lease_seconds": disp.lease}

    @app.post("/flows/v1/next")
    async def next_job(request: Request, x_xqo_worker: str | None = Header(default=None),
                       authorization: str | None = Header(default=None)):
        w = auth(x_xqo_worker, authorization)
        d = await body(request)
        try:
            wait = float(d.get("wait", 20))
        except (TypeError, ValueError):
            wait = 20.0
        job = await disp.next_job(w, wait, str(d.get("version") or "")[:40])
        return {"job": job.for_worker() if job else None, "lease_seconds": disp.lease}

    @app.post("/flows/v1/jobs/{job_id}/heartbeat")
    async def heartbeat(job_id: str, request: Request, x_xqo_worker: str | None = Header(default=None),
                        authorization: str | None = Header(default=None)):
        w = auth(x_xqo_worker, authorization)
        d = await body(request)
        return disp.heartbeat(w, job_id, d.get("progress") if isinstance(d.get("progress"), dict) else None)

    @app.post("/flows/v1/jobs/{job_id}/result")
    async def result(job_id: str, request: Request, x_xqo_worker: str | None = Header(default=None),
                     authorization: str | None = Header(default=None)):
        w = auth(x_xqo_worker, authorization)
        return disp.result(w, job_id, await body(request))

    if service is not None:
        @app.post("/flows/v1/hooks/{flow}/{tid}")
        async def hook(flow: str, tid: str, request: Request):
            return await hook_response(service, flow, tid, request)

    return app


async def hook_response(service, flow: str, tid: str, request: Request):
    """Shared by the public listener and the dashboard route: 202 {run_id} or an error status."""
    from .triggers import MAX_HOOK_BODY
    raw = b""
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MAX_HOOK_BODY:
            raise HTTPException(413, "body too large")
    headers = {k.lower(): v for k, v in request.headers.items()}
    try:
        run = service.hook(flow, tid, raw, headers)
    except KeyError:
        raise HTTPException(404, "no such webhook")
    except PermissionError as e:
        raise HTTPException(401, str(e))
    except OverflowError as e:
        raise HTTPException(429, str(e) or "too many requests")
    except FlowError as e:
        raise HTTPException(422, str(e))
    from fastapi.responses import JSONResponse
    if run is None:
        return JSONResponse({"run_id": None, "skipped": True}, status_code=202)
    return JSONResponse({"run_id": run.id, "status": run.status}, status_code=202)
