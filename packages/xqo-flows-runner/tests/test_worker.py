"""End to end: an HQ (dashboard + worker API on real ports) and a headless worker with its own agents."""
import asyncio
import json
import socket
import threading
import time
import urllib.request

import pytest
import uvicorn

from xqo.api import build
from xqo.web import create_app
from xqo_flows.workers import create_worker_app
from xqo_flows_runner import HQClient, HQError, Worker, settings

TOKEN = "s3cret-token-for-tests"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def node(nid, type_, **data):
    return {"id": nid, "type": type_, "data": data}


def edge(s, t, port=None):
    return {"sourceNodeID": s, "targetNodeID": t, **({"sourcePortID": port} if port else {})}


class HQ:
    """xqo serve in a thread: the dashboard app and the flows worker API on one event loop."""

    def __init__(self, tmp_path, monkeypatch, lease=60):
        monkeypatch.setenv("XQO_WORKER_GPU_TOKEN", TOKEN)
        self.wport, self.port = free_port(), free_port()
        cfg = tmp_path / "hq.json"
        cfg.write_text(json.dumps({
            "agents": [{"name": "planner2", "skills": ["python"]}],
            "flows": {"examples": False, "workers": {"listen": f"127.0.0.1:{self.wport}", "lease_seconds": lease,
                                                     "allow": {"gpu": {"labels": ["gpu", "linux"]}}}}}))
        self.bus = build(cfg, tmp_path / "hq" / "artifacts", str(tmp_path / "hq" / "logs" / "e.jsonl"), mock=True,
                         departments="none")
        disp = self.bus.plugins.service("flows").dispatcher
        self.servers = [uvicorn.Server(uvicorn.Config(create_app(self.bus), "127.0.0.1", self.port, log_level="warning")),
                        uvicorn.Server(uvicorn.Config(create_worker_app(disp, self.bus.plugins.service("flows")), "127.0.0.1", self.wport, log_level="warning"))]
        self.thread = threading.Thread(target=lambda: asyncio.run(self._serve()), daemon=True)

    async def _serve(self):
        await asyncio.gather(*(s.serve() for s in self.servers))

    def __enter__(self):
        self.thread.start()
        t0 = time.time()
        while not all(s.started for s in self.servers):
            assert time.time() - t0 < 10, "HQ did not start"
            time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        for s in self.servers:
            s.should_exit = True
        self.thread.join(10)

    def api(self, method, path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    def wait_run(self, rid, timeout=30):
        t0 = time.time()
        while time.time() - t0 < timeout:
            r = self.api("GET", f"/api/flows/runs/{rid}")
            if r["status"] in ("done", "error", "cancelled", "interrupted"):
                return r
            time.sleep(0.1)
        raise AssertionError(f"run still {r['status']}: {r}")


class WorkerThread:
    def __init__(self, tmp_path, hq, name="gpu", token=TOKEN, agents=None):
        cfg = tmp_path / f"{name}.json"
        cfg.write_text(json.dumps({"agents": agents or [{"name": "builder", "skills": ["python"]}]}))
        self.bus = build(cfg, tmp_path / name / "artifacts", str(tmp_path / name / "logs" / "e.jsonl"), mock=True,
                         departments="none")
        self.client = HQClient(f"http://127.0.0.1:{hq.wport}", name, token)
        self.logs = []
        self.worker = None
        self.error = None
        self.thread = threading.Thread(target=self._go, daemon=True)

    def _go(self):
        async def main():
            self.worker = Worker(self.bus, self.client, jobs=2, poll_wait=1, log=self.logs.append)
            self.loop = asyncio.get_running_loop()
            await self.worker.ping()
            await self.worker.run()
        try:
            asyncio.run(main())
        except Exception as e:
            self.error = e

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        if self.worker:
            self.loop.call_soon_threadsafe(self.worker.stop)
        self.thread.join(15)


def remote_step_flow():
    return {"nodes": [node("s", "start", inputs=[{"name": "what", "default": "a parser"}]),
                      node("local", "agent", prompt="plan {{inputs.what}}"),
                      node("far", "remote", labels=["gpu"], agent="builder", prompt="build {{inputs.what}} after:\n{{local.result}}"),
                      node("e", "end", output="REMOTE {{far.result}} via {{far.worker}}")],
            "edges": [edge("s", "local"), edge("local", "far"), edge("far", "e")]}


def test_remote_step_runs_on_the_worker_with_its_own_agents(tmp_path, monkeypatch):
    with HQ(tmp_path, monkeypatch) as hq, WorkerThread(tmp_path, hq) as w:
        hq.api("PUT", "/api/flows/build", remote_step_flow())
        rid = hq.api("POST", "/api/flows/build/run", {"inputs": {"what": "a lexer"}})["id"]
        r = hq.wait_run(rid)
        assert r["status"] == "done", r
        far = r["nodes"]["far"]
        assert far["worker"] == "gpu" and far["job_id"]
        task = w.bus.tasks[far["output"]["task_id"]]  # the step ran as a task on the worker
        assert task.assignee == "builder" and "a lexer" in task.prompt
        assert r["result"].startswith("REMOTE ")
        assert r["nodes"]["local"]["task_id"] in hq.bus.tasks  # the local step stayed on the HQ
        st = hq.api("GET", "/api/flows/workers")
        assert st["workers"][0]["online"] and st["workers"][0]["labels"] == ["gpu", "linux"]


def test_whole_flow_runs_on_a_worker_and_reports_nodes(tmp_path, monkeypatch):
    flow = {"nodes": [node("s", "start"), node("a", "agent", prompt="one"), node("b", "agent", agent="builder", prompt="two {{a.status}}"),
                      node("e", "end", output="W {{b.status}}")],
            "edges": [edge("s", "a"), edge("a", "b"), edge("b", "e")]}
    with HQ(tmp_path, monkeypatch) as hq, WorkerThread(tmp_path, hq) as w:
        hq.api("PUT", "/api/flows/whole", flow)
        rid = hq.api("POST", "/api/flows/whole/run", {"labels": ["linux"]})["id"]
        r = hq.wait_run(rid)
        assert r["status"] == "done" and r["result"] == "W done" and r["where"] == "worker:gpu"
        assert r["nodes"]["b"]["status"] == "done" and r["nodes"]["b"]["task_id"] in w.bus.tasks
        assert not any(t.assignee == "planner2" for t in hq.bus.tasks.values())  # nothing ran on the HQ


def approval_flow():
    return {"nodes": [node("s", "start"), node("a", "agent", agent="builder", prompt="draft"),
                      node("ok", "approval", question="ship {{a.status}}?"),
                      node("b", "agent", agent="builder", prompt="ship"), node("e", "end", output="SHIPPED {{b.status}}"),
                      node("no", "end", output="REJECTED")],
            "edges": [edge("s", "a"), edge("a", "ok"), edge("ok", "b", "approve"), edge("b", "e"),
                      edge("ok", "no", "reject")]}


def wait_status(hq, rid, status, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = hq.api("GET", f"/api/flows/runs/{rid}")
        if r["status"] == status:
            return r
        time.sleep(0.1)
    raise AssertionError(f"run is {r['status']}, not {status}: {r}")


def test_whole_flow_with_an_approval_asks_on_the_hq(tmp_path, monkeypatch):
    with HQ(tmp_path, monkeypatch) as hq, WorkerThread(tmp_path, hq) as w:
        hq.api("PUT", "/api/flows/ap", approval_flow())
        rid = hq.api("POST", "/api/flows/ap/run", {"remote": True})["id"]
        r = wait_status(hq, rid, "waiting")
        assert r["nodes"]["ok"]["status"] == "waiting" and r["nodes"]["ok"]["question"] == "ship done?"
        pend = hq.api("GET", "/api/flows/approvals")
        assert [(p["run_id"], p["node"]) for p in pend] == [(rid, "ok")]
        hq.api("POST", f"/api/flows/runs/{rid}/approve", {"node": "ok", "approve": True})
        r = hq.wait_run(rid)
        assert r["status"] == "done" and r["result"] == "SHIPPED done", r
        assert r["nodes"]["ok"]["answer"] == "approve" and r["nodes"]["no"]["status"] == "skipped"
        assert r["where"] == "worker:gpu" and r["nodes"]["b"]["task_id"] in w.bus.tasks
        assert hq.api("GET", "/api/flows/approvals") == []


def test_whole_flow_approval_times_out_on_the_worker(tmp_path, monkeypatch):
    flow = approval_flow()
    flow["nodes"][2]["data"].update(timeout=0.5, default="reject")
    with HQ(tmp_path, monkeypatch) as hq, WorkerThread(tmp_path, hq):
        hq.api("PUT", "/api/flows/ap", flow)
        rid = hq.api("POST", "/api/flows/ap/run", {"remote": True})["id"]
        r = hq.wait_run(rid)
        assert r["status"] == "done" and r["result"] == "REJECTED", r
        assert r["nodes"]["ok"]["answered_by"] == "timeout"


def test_whole_flow_run_resumes_on_a_worker_keeping_done_steps(tmp_path, monkeypatch):
    with HQ(tmp_path, monkeypatch) as hq, WorkerThread(tmp_path, hq) as w:
        hq.api("PUT", "/api/flows/ap", approval_flow())
        rid = hq.api("POST", "/api/flows/ap/run", {"labels": ["gpu"]})["id"]
        wait_status(hq, rid, "waiting")
        first = hq.api("GET", f"/api/flows/runs/{rid}")["nodes"]["a"]["task_id"]
        r = hq.api("POST", f"/api/flows/runs/{rid}/cancel")
        r = hq.wait_run(rid)
        assert r["status"] == "cancelled" and r["resumable"]
        r = hq.api("POST", f"/api/flows/runs/{rid}/resume")
        assert r["status"] == "running" and r["resumed"] == 1 and not r["resumable"]
        wait_status(hq, rid, "waiting")
        hq.api("POST", f"/api/flows/runs/{rid}/approve", {"node": "ok", "approve": True})
        r = hq.wait_run(rid)
        assert r["status"] == "done" and r["result"] == "SHIPPED done", r
        assert r["nodes"]["a"]["task_id"] == first  # the done step did not run again
        assert sum(1 for t in w.bus.tasks.values() if t.prompt == "draft") == 1


def test_webhook_on_the_public_listener(tmp_path, monkeypatch):
    import hashlib
    import hmac as _hmac
    monkeypatch.setenv("HOOK_SECRET", "hk")
    flow = {"nodes": [node("s", "start", inputs=[{"name": "msg", "default": ""}]),
                      node("a", "agent", prompt="handle {{inputs.msg}}"), node("e", "end", output="{{inputs.msg}}")],
            "edges": [edge("s", "a"), edge("a", "e")],
            "triggers": [{"id": "gh", "type": "webhook", "secret_env": "HOOK_SECRET",
                          "inputs": {"msg": "{{body.action}}"}}]}
    with HQ(tmp_path, monkeypatch) as hq:
        hq.api("PUT", "/api/flows/hooked", flow)
        body = b'{"action": "opened"}'
        sig = "sha256=" + _hmac.new(b"hk", body, hashlib.sha256).hexdigest()
        url = f"http://127.0.0.1:{hq.wport}/flows/v1/hooks/hooked/gh"
        req = urllib.request.Request(url, data=body, method="POST", headers={"X-Hub-Signature-256": sig})
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert resp.status == 202
            rid = json.loads(resp.read())["run_id"]
        r = hq.wait_run(rid)
        assert r["status"] == "done" and "opened" in r["result"] and "<untrusted_webhook_data>" in r["result"]
        assert r["trigger"] == {"id": "gh", "type": "webhook"}
        bad = urllib.request.Request(url, data=body, method="POST", headers={"X-Hub-Signature-256": "sha256=00"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(bad, timeout=10)
        assert e.value.code == 401


def test_labels_no_worker_has_fail_at_once(tmp_path, monkeypatch):
    flow = remote_step_flow()
    flow["nodes"][2]["data"]["labels"] = ["tpu"]
    with HQ(tmp_path, monkeypatch) as hq:
        saved = hq.api("PUT", "/api/flows/f", flow)
        assert "no worker" in saved["warnings"][0]
        with pytest.raises(urllib.error.HTTPError):
            hq.api("POST", "/api/flows/f/run", {})


def test_wrong_token_and_unknown_worker_are_refused(tmp_path, monkeypatch):
    with HQ(tmp_path, monkeypatch) as hq:
        for name, token, code in (("gpu", "wrong", 401), ("nobody", TOKEN, 401)):
            with pytest.raises(HQError) as e:
                HQClient(f"http://127.0.0.1:{hq.wport}", name, token).call("GET", "/flows/v1/ping")
            assert e.value.status == code
        assert HQClient(f"http://127.0.0.1:{hq.wport}", "gpu", TOKEN).call("GET", "/flows/v1/ping")["labels"] == ["gpu", "linux"]


def test_cancel_on_the_hq_cancels_the_task_on_the_worker(tmp_path, monkeypatch):
    with HQ(tmp_path, monkeypatch, lease=0.6) as hq, WorkerThread(tmp_path, hq) as w:
        async def slow(task):
            await asyncio.sleep(60)
        w.bus.agents["builder"].handle = slow
        flow = remote_step_flow()
        flow["nodes"][1]["data"]["prompt"] = "x"
        hq.api("PUT", "/api/flows/slow", flow)
        rid = hq.api("POST", "/api/flows/slow/run", {})["id"]
        t0 = time.time()
        while hq.api("GET", f"/api/flows/runs/{rid}")["nodes"]["far"].get("status") != "running" or \
                not any(t.assignee == "builder" for t in w.bus.tasks.values()):
            assert time.time() - t0 < 20
            time.sleep(0.1)
        assert hq.api("POST", f"/api/flows/runs/{rid}/cancel")["status"] == "cancelled"
        t0 = time.time()
        while not any(t.status == "cancelled" for t in w.bus.tasks.values() if t.assignee == "builder"):
            assert time.time() - t0 < 20, w.logs
            time.sleep(0.1)
        job = hq.api("GET", "/api/flows/workers")["jobs"][0]
        t0 = time.time()
        while job["status"] == "claimed":
            assert time.time() - t0 < 20
            time.sleep(0.1)
            job = hq.api("GET", "/api/flows/workers")["jobs"][0]
        assert job["status"] == "cancelled"


def test_a_lost_worker_gives_the_job_back_and_another_takes_it(tmp_path, monkeypatch):
    with HQ(tmp_path, monkeypatch, lease=0.5) as hq:
        hq.api("PUT", "/api/flows/build", remote_step_flow())
        rid = hq.api("POST", "/api/flows/build/run", {})["id"]
        c = HQClient(f"http://127.0.0.1:{hq.wport}", "gpu", TOKEN)
        job = None
        t0 = time.time()
        while not job:  # a worker takes the job and then disappears (no heartbeat, no result)
            job = c.call("POST", "/flows/v1/next", {"wait": 1})["job"]
            assert time.time() - t0 < 20
        with WorkerThread(tmp_path, hq) as w:
            r = hq.wait_run(rid)
            assert r["status"] == "done", r
        jobs = hq.api("GET", "/api/flows/workers")["jobs"]
        assert jobs[0]["attempts"] == 2


def test_worker_settings():
    import os
    os.environ["T_TOK"] = "x"
    s = settings({"flows_worker": {"hq": "http://h:8200", "name": "w1", "token_env": "T_TOK", "jobs": 3}})
    assert s["jobs"] == 3 and s["token"] == "x"
    with pytest.raises(ValueError, match="plaintext"):
        settings({"flows_worker": {"hq": "h", "name": "n", "token": "abc"}})
    with pytest.raises(ValueError, match="--hq"):
        settings({})
    with pytest.raises(ValueError, match="set NOPE_TOK"):
        settings({"flows_worker": {"hq": "h", "name": "n", "token_env": "NOPE_TOK"}})


def test_worker_command_is_registered():
    import subprocess
    import sys
    out = subprocess.run([sys.executable, "-m", "xqo", "worker", "--help"], capture_output=True, text=True)
    assert out.returncode == 0 and "--hq" in out.stdout
