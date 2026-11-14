"""Cancelling running tasks and deleting finished ones (trash, undo, purge, retention)."""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from xqo.config import build
from xqo.sandbox import Sandbox, SandboxConfig
from xqo.web import create_app

ROOT = Path(__file__).resolve().parent.parent


def make(tmp_path):
    return build(ROOT / "agents.json", tmp_path / "art", str(tmp_path / "logs" / "ev.jsonl"), mock=True)


async def until(cond, timeout=10):
    t0 = time.monotonic()
    while not cond():
        if time.monotonic() - t0 > timeout:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.02)


def kinds(b, root):
    return [e["kind"] for e in b.task_events(root)]


# ------------------------------------------------------------------ cancel
def test_cancel_while_waiting_on_user(tmp_path):
    b = make(tmp_path)

    async def go():
        job = asyncio.create_task(b.submit("Please confirm, then build fib"))
        await until(lambda: b.attention.pending())
        root = next(t for t in b.tasks.values() if t.parent_id is None)
        assert b.is_running(root.id)
        t = await b.cancel(root.id, "user", "changed my mind")
        assert (await job) is t
        return t

    t = asyncio.run(go())
    assert t.status == "cancelled" and "changed my mind" in t.result
    assert not b.attention.pending() and not b.is_running(t.id)
    att = next(iter(b.attention.items.values()))
    assert att.status == "cancelled"
    ks = kinds(b, t.id)
    assert ks.index("task_cancel_requested") < ks.index("task_cancelled")
    ev = next(e for e in b.task_events(t.id) if e["kind"] == "task_cancelled")
    assert ev["meta"]["by"] == "user" and ev["meta"]["reason"] == "changed my mind" and t.id in ev["meta"]["stopped"]
    with pytest.raises(ValueError):  # not running any more
        asyncio.run(b.cancel(t.id))
    # state survives a restart, and the cancelled task can still be forked
    b2 = make(tmp_path)
    assert b2.tasks[t.id].status == "cancelled"
    first = b2.task_events(t.id)[0]
    fork = asyncio.run(b2.fork(t.id, first["id"], "build fib"))
    assert fork.forked_from["task_id"] == t.id


def test_cancel_mid_handover_keeps_artifacts_and_commits(tmp_path, monkeypatch):
    b = make(tmp_path)
    reviewer = b.agents["reviewer"]
    orig = reviewer.llm.create

    async def slow(**kw):  # the reviewer hangs: cancel arrives while it is working
        if kw.get("agent") == "reviewer":
            await asyncio.sleep(60)
        return await orig(**kw)

    monkeypatch.setattr(reviewer.llm, "create", slow)

    async def go():
        job = asyncio.create_task(b.submit("Build fib"))
        await until(lambda: any(t.assignee == "reviewer" and t.status == "running" for t in b.tasks.values()))
        root = next(t for t in b.tasks.values() if t.parent_id is None)
        await b.cancel(root.id)
        return await job

    t = asyncio.run(go())
    assert t.status == "cancelled"
    rev = next(x for x in b.tasks.values() if x.assignee == "reviewer")
    assert rev.status == "cancelled"
    assert (b.artifacts.workspace(t.id) / "src" / "fib.py").is_file()  # work so far is kept
    log = b.artifacts.git_log(t.id)
    if b.artifacts.git and b.artifacts.git.enabled:
        assert log[0]["message"].startswith("Task cancelled by user")
        ev = next(e for e in b.task_events(t.id) if e["kind"] == "task_cancelled")
        assert ev["meta"]["commit"] == log[0]["sha"]


@pytest.mark.skipif(os.name == "nt", reason="process groups")
def test_cancel_kills_sandbox_process_tree(tmp_path):
    pidf = tmp_path / "pid"
    cmd = f"{sys.executable} -c \"import os,time; open('{pidf}','w').write(str(os.getpid())); time.sleep(60)\""

    async def go():
        job = asyncio.create_task(Sandbox(SandboxConfig(backend="local", timeout=90, venv=False)).run(tmp_path, cmd))
        await until(lambda: pidf.exists() and pidf.read_text())
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job

    asyncio.run(go())
    pid = int(pidf.read_text())
    time.sleep(0.2)
    status = Path(f"/proc/{pid}/status")
    if status.exists():  # an unreaped zombie still has an entry, but it is not running
        assert "\nState:\tZ" in status.read_text()
    else:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


# ------------------------------------------------------------------ delete
def finished(tmp_path):
    b = make(tmp_path)
    t = asyncio.run(b.submit("Build fib"))
    return b, t


def test_soft_delete_hides_and_undo_restores(tmp_path):
    b, t = finished(tmp_path)
    n_art = len(b.artifacts.list(t.id))
    (b.artifacts.env_dir(t.id)).mkdir(parents=True)  # a venv is dropped, not kept in the trash
    e = b.lifecycle.delete(t.id)
    assert e["artifacts"] == n_art and e["purge_at"] > time.time()
    assert t.id not in b.tasks and not b.artifacts.list(t.id) and not (b.artifacts.root / t.id).exists()
    assert not any(x.get("root_task_id") == t.id and x["kind"] != "task_deleted" for x in b.history)
    assert not (b.lifecycle.dir / t.id / t.id / ".venv").exists()
    assert make(tmp_path).tasks.get(t.id) is None  # still hidden after a restart
    b.lifecycle.restore(t.id)
    assert b.tasks[t.id].status == "done" and len(b.artifacts.list(t.id)) == n_art
    assert (b.artifacts.workspace(t.id) / "src" / "fib.py").is_file() and not b.lifecycle.trash_entries()
    assert b.artifacts.git_log(t.id) or not (b.artifacts.git and b.artifacts.git.enabled)
    assert "task_restored" in [x["kind"] for x in b.history]


def test_permanent_delete_and_retention(tmp_path):
    b, t = finished(tmp_path)
    b.lifecycle.delete(t.id, permanent=True)
    assert not (b.artifacts.root / t.id).exists() and not b.lifecycle.trash_entries()
    lines = [json.loads(l) for l in b.log_path.read_text().splitlines()]
    mine = [d for d in lines if d.get("root_task_id") == t.id]
    assert [d["kind"] for d in mine] == ["task_deleted"] and mine[0]["meta"]["mode"] == "permanent"
    assert b.task_events(t.id) == []
    # retention: a second task in the trash past its time is purged on the next check
    t2 = asyncio.run(b.submit("Build fib again"))
    b.lifecycle.delete(t2.id)
    assert b.lifecycle.purge_expired() == []
    assert b.lifecycle.purge_expired(now=time.time() + 8 * 86400) == [t2.id]
    assert not b.lifecycle.trash_entries() and b.task_events(t2.id) == []


def test_delete_rules_and_branches(tmp_path):
    b, t = finished(tmp_path)
    child = next(x for x in b.tasks.values() if x.parent_id)
    with pytest.raises(ValueError, match="sub-task"):
        b.lifecycle.delete(child.id)
    with pytest.raises(KeyError):
        b.lifecycle.delete("task_nope")
    fork = asyncio.run(b.fork(t.id, b.task_events(t.id)[0]["id"]))
    e = b.lifecycle.delete(t.id)
    assert e["branches"] == [{"id": fork.id, "name": fork.name}]
    assert fork.id in b.tasks and b.tasks[fork.id].forked_from["task_id"] == t.id  # branch kept
    b.lifecycle.purge(t.id)
    assert fork.id in b.tasks

    async def running():
        job = asyncio.create_task(b.submit("Please confirm, then build fib"))
        await until(lambda: b.attention.pending())
        root = next(x for x in b.tasks.values() if x.parent_id is None and x.status == "needs_attention")
        with pytest.raises(ValueError, match="cancel it first"):
            b.lifecycle.delete(root.id)
        await b.cancel(root.id)
        await job
        b.lifecycle.delete(root.id)

    asyncio.run(running())


def test_api_and_cli(tmp_path, monkeypatch, capsys):
    b, t = finished(tmp_path)
    t2 = asyncio.run(b.submit("Build fib again"))
    with TestClient(create_app(b)) as c:
        assert c.post(f"/api/tasks/{t.id}/cancel").status_code == 409
        assert c.post("/api/tasks/task_nope/cancel").status_code == 404
        r = c.post("/api/tasks/delete", json={"ids": [t.id, t2.id, "task_nope"]}).json()
        assert [x["ok"] for x in r] == [True, True, False] and r[2]["status"] == 404
        assert {x["root_id"] for x in c.get("/api/trash").json()["items"]} == {t.id, t2.id}
        assert t.id not in {x["id"] for x in c.get("/api/tasks").json()}
        assert c.get(f"/api/tasks/{t.id}/events").status_code == 404
        assert c.post(f"/api/trash/{t.id}/restore").status_code == 200
        assert c.get(f"/api/tasks/{t.id}").status_code == 200
        assert c.delete(f"/api/trash/{t2.id}").json()["permanent"]
        assert c.delete(f"/api/tasks/{t.id}?permanent=true").json()["permanent"]
        assert c.get("/api/trash").json()["items"] == []
    # CLI without a server works on the data dir
    from xqo import __main__ as cli
    data = tmp_path / "d"
    b3 = build(ROOT / "agents.json", data / "artifacts", str(data / "logs" / "events.jsonl"), mock=True)
    t4 = asyncio.run(b3.submit("Build fib"))
    base = ["xqo", "--config", str(ROOT / "agents.json"), "--data-dir", str(data)]
    off = ["--url", "http://127.0.0.1:9"]  # nothing listens there
    monkeypatch.setattr(sys, "argv", base + ["delete", t4.id] + off)
    cli.main()
    assert "moved to the trash" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", base + ["restore", t4.id] + off)
    cli.main()
    assert "restored" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", base + ["cancel", t4.id] + off)
    with pytest.raises(SystemExit, match="no server"):
        cli.main()


# ------------------------------------------------------------------ departments
def test_department_cancel_and_delete(tmp_path):
    import shutil
    shutil.copy(ROOT / "agents.json", tmp_path / "agents.json")
    shutil.copytree(ROOT / "departments", tmp_path / "departments")
    p = tmp_path / "departments" / "engineering.json"
    d = json.loads(p.read_text())
    p.write_text(json.dumps({**d, "budget": {"max_tokens": 1}}))  # engineering stops to ask about its budget
    b = build(tmp_path / "agents.json", tmp_path / "art", str(tmp_path / "logs" / "ev.jsonl"), mock=True)

    async def go():
        job = asyncio.create_task(b.submit("Build fib"))
        await until(lambda: any(a.kind == "budget" for a in b.attention.pending()))
        root = next(t for t in b.tasks.values() if t.parent_id is None)
        await b.cancel(root.id, "user", "too expensive")
        return await job

    t = asyncio.run(go())
    assert t.status == "cancelled"
    eng = [x for x in b.tasks.values() if x.assignee == "eng_lead"]
    assert eng and all(x.status == "cancelled" for x in eng)  # the cross-department handover ended
    assert all(a.status != "pending" for a in b.attention.items.values())
    ev = next(e for e in b.task_events(t.id) if e["kind"] == "task_cancelled")
    assert ev["meta"]["usage"]["engineering"]["tokens"] > 0  # usage up to the cancel is kept
    # the department's own workspace and notes go to the trash with the task, and come back
    dkey = f"{t.id}.engineering"
    b.artifacts.write_file(dkey, "draft/a.py", "x = 1\n", t.id, "eng_lead")
    b.memory.set(dkey, "k", "v", "eng_lead")
    e = b.lifecycle.delete(t.id)
    assert sorted(e["folders"]) == sorted([t.id, dkey])
    assert not (b.artifacts.root / dkey).exists() and not b.artifacts.list(dkey)
    b.lifecycle.restore(t.id)
    assert b.artifacts.read_file(dkey, "draft/a.py") == "x = 1\n" and b.memory.notes(dkey)["k"]["value"] == "v"
    b.lifecycle.delete(t.id, permanent=True)
    assert not (b.artifacts.root / dkey).exists() and not b.artifacts.list(dkey)
    assert not [k for k in b.budget.used if k[0] == t.id] and b.budget.totals["engineering"]["tokens"] > 0
