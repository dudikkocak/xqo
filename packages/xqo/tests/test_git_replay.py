"""Git history per workspace, replay log, and forking a task from any event."""
import asyncio
import io
import subprocess
import sys
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from xqo.artifacts import ArtifactStore
from xqo.bus import MessageBus
from xqo.config import build
from xqo.gitstore import GitConfig, GitError, GitRepo, git_available
from xqo.patching import PatchError
from xqo.sandbox import Sandbox, SandboxConfig
from xqo.web import create_app

pytestmark = pytest.mark.skipif(not git_available(), reason="git not installed")


def store(tmp_path, **git):
    s = ArtifactStore(tmp_path / "artifacts")
    s.git = GitRepo(GitConfig(**git))
    return s


def git(ws, *args):
    return subprocess.run(["git", *args], cwd=ws, capture_output=True, text=True, check=True).stdout


# ------------------------------------------------------------------ commits
def test_one_commit_per_version_authored_by_agent(tmp_path):
    s = store(tmp_path)
    a = s.write_file("r", "src/a.py", "x = 1\n", "t1", "coder")
    assert len(a["commit"]) == 40
    assert "commit" not in s.write_file("r", "src/a.py", "x = 1\n", "t1", "coder")  # unchanged: no commit
    s.patch_file("r", "src/a.py", "t2", "reviewer", edits=[{"old": "x = 1", "new": "x = 2"}])
    s.rollback("r", "src/a.py", a["path"])
    s.delete_file("r", "src/a.py", "t2", "reviewer")
    log = s.git_log("r")
    assert [c["message"] for c in log] == [
        "delete src/a.py by reviewer (t2)", "rollback src/a.py by user (t1)",
        "patch src/a.py by reviewer (t2)", "create src/a.py by coder (t1)"]
    assert [c["author"] for c in log] == ["reviewer", "user", "reviewer", "coder"]
    assert log[0]["files"] == [{"status": "D", "file": "src/a.py"}]
    assert s.git.show(s.workspace("r"), log[2]["sha"], "src/a.py") == "x = 2\n"
    assert git(s.workspace("r"), "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"


def test_git_folder_is_hidden_and_protected(tmp_path):
    s = store(tmp_path)
    s.write_file("r", "a.txt", "hi", "t", "coder")
    assert [f["file"] for f in s.list_files("r")] == ["a.txt"]
    names = zipfile.ZipFile(io.BytesIO(s.zip_task("r", workspace_only=True))).namelist()
    assert names == ["a.txt"]
    assert s.resolve("r/workspace/.git/config") is None
    for bad in (".git/config", ".GIT/hooks/pre-commit"):
        with pytest.raises(PatchError):
            s.write_file("r", bad, "evil", "t", "coder")
    r = asyncio.run(Sandbox(SandboxConfig(backend="local", timeout=10)).run(
        s.workspace("r"), f"{sys.executable} -c \"import os; print(sorted(os.listdir('.')))\""))
    assert r["stdout"].strip() == "['a.txt']"


def test_git_disabled(tmp_path, monkeypatch):
    s = store(tmp_path, enabled=False)
    assert "commit" not in s.write_file("r", "a.txt", "hi", "t", "coder")
    assert not (s.workspace("r") / ".git").exists() and s.git_log("r") == []
    monkeypatch.setenv("XQO_GIT", "0")
    assert GitConfig.from_dict({"enabled": True}).enabled is False


def test_git_failure_never_breaks_writes(tmp_path, monkeypatch):
    s = store(tmp_path)
    monkeypatch.setattr(s.git, "commit", lambda *a, **k: (_ for _ in ()).throw(GitError("boom")))
    rec = s.write_file("r", "a.txt", "hi", "t", "coder")
    assert rec["git_error"] == "boom" and s.read_file("r", "a.txt") == "hi"


# ------------------------------------------------------------------ push
def test_push_to_local_remote(tmp_path):
    s = store(tmp_path)
    s.write_file("r", "a.txt", "hi", "t", "coder")
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    res = s.git.push(s.workspace("r"), str(bare), "feature/x")
    assert git(bare, "rev-parse", "feature/x").strip() == res["head"]


def test_push_validation_and_token_never_leaks(tmp_path, monkeypatch):
    s = store(tmp_path)
    s.write_file("r", "a.txt", "hi", "t", "coder")
    ws = s.workspace("r")
    with pytest.raises(GitError, match="remote URL"):
        s.git.push(ws, "")
    with pytest.raises(GitError, match="credentials"):
        s.git.push(ws, "https://user:secret@example.invalid/repo.git")
    with pytest.raises(GitError, match="branch"):
        s.git.push(ws, str(tmp_path / "x.git"), "../evil")
    monkeypatch.setenv("XQO_GIT_TOKEN", "tok_SUPERSECRET")
    with pytest.raises(GitError) as e:
        s.git.push(ws, "https://127.0.0.1:9/repo.git")
    assert "SUPERSECRET" not in str(e.value) and "eC1hY2Nlc3MtdG9rZW46" not in str(e.value)
    assert "SUPERSECRET" not in (ws / ".git" / "config").read_text()
    with pytest.raises(GitError, match="no git history"):
        s.git.push(s.workspace("other"), str(tmp_path / "x.git"))


# ------------------------------------------------------------------ replay & fork
def mock_bus(tmp_path):
    return build("agents.json", tmp_path / "artifacts", str(tmp_path / "logs" / "events.jsonl"), mock=True)


def test_task_events_read_full_log(tmp_path, monkeypatch):
    from xqo import bus as busmod
    b = mock_bus(tmp_path)
    t = asyncio.run(b.submit("Build fib"))
    monkeypatch.setattr(busmod, "HISTORY_LIMIT", 3)
    events = b.task_events(t.id)
    assert events[0]["kind"] == "task" and events[-1]["kind"] == "done"
    assert all(e["root_task_id"] == t.id for e in events)
    assert len(events) > 10 and b.task_events("task_nope") == []


def test_transcript_is_capped_but_keeps_request():
    ev = [{"kind": "task", "sender": "user", "recipient": "planner", "task_id": "t", "ts": "2026-01-01T00:00:00Z",
           "content": "ORIGINAL REQUEST"}]
    ev += [{"kind": "direct", "sender": "a", "recipient": "b", "task_id": "t", "ts": "2026-01-01T00:00:01Z",
            "content": f"msg {i} " + "x" * 500} for i in range(200)]
    ev.append({"kind": "llm", "sender": "a", "task_id": "t", "ts": "2026-01-01T00:00:02Z", "content": "LLMCALL"})
    text = MessageBus.transcript(ev, limit=5000)
    assert len(text) <= 5200 and "ORIGINAL REQUEST" in text and "msg 199" in text
    assert "events omitted" in text and "LLMCALL" not in text


def test_fork_restores_workspace_notes_and_context(tmp_path):
    b = mock_bus(tmp_path)
    seen = []
    orig_handle = b.agents["planner"].handle

    async def spy(task):
        seen.append(task)
        return await orig_handle(task)
    b.agents["planner"].handle = spy

    async def go():
        t = await b.submit("Build fib with run tests")
        ev = b.task_events(t.id)
        arts = [e for e in ev if e["kind"] == "artifact" and e["meta"].get("file")]
        first = arts[0]  # coder created src/fib.py; tests not written yet
        f = await b.fork(t.id, first["id"], "Build fib, but recursive")
        return t, f, first, arts

    t, f, first, arts = asyncio.run(go())
    assert [x["file"] for x in b.artifacts.list_files(f.id)] == ["src/fib.py"]
    assert b.artifacts.read_file(f.id, "src/fib.py") == b.artifacts.resolve(first["meta"]["path"]).read_text()
    assert set(b.memory.notes(f.id)) == set(b.memory.notes(t.id))  # note was taken before the fork point
    assert f.forked_from == {"task_id": t.id, "task_name": t.name, "event_id": first["id"],
                             "event_kind": "artifact", "event_ts": first["ts"]}
    assert f.prompt == "Build fib, but recursive" and f.assignee == "planner"
    assert "fork of an earlier run" in f.context and "Build fib with run tests" in f.context
    assert f.id != t.id and b.artifacts.task_meta(f.id)["forked_from"]["task_id"] == t.id
    log = b.artifacts.git_log(f.id)
    assert len(log) == 1 and log[0]["message"].startswith(f"fork of {t.id}")
    fk = [e for e in b.task_events(f.id) if e["kind"] == "fork"]
    assert fk and fk[0]["meta"]["files"] == ["src/fib.py"]
    assert b.artifacts.history(f.id, "src/fib.py")[0]["action"] == "fork"  # viewer/rollback see it

    done = asyncio.run(b._run(f))
    assert done.status == "done"
    assert seen[-1] is f
    # the original is untouched
    assert {x["file"] for x in b.artifacts.list_files(t.id)} == {"src/fib.py", "tests/test_fib.py"}

    # forked_from survives a restart
    b2 = MessageBus(b.log_path)
    assert b2.tasks[f.id].forked_from["event_id"] == first["id"]

    # fork at a later event sees the patched file
    patched = next(e for e in arts if e["meta"]["action"] == "patch")
    f2 = asyncio.run(b.fork(t.id, patched["id"]))
    assert f2.prompt == t.prompt
    assert b.artifacts.read_file(f2.id, "src/fib.py") == b.artifacts.read_file(t.id, "src/fib.py")
    with pytest.raises(KeyError):
        asyncio.run(b.fork(t.id, "evt_missing"))


def test_context_reaches_the_model(tmp_path):
    b = mock_bus(tmp_path)
    calls = []
    llm = b.agents["planner"].llm
    orig = llm.create

    async def spy(**kw):
        calls.append(kw["messages"][0]["content"])
        return await orig(**kw)
    llm.create = spy

    async def go():
        t = await b.submit("Build fib")
        e = b.task_events(t.id)[1]
        f = await b.fork(t.id, e["id"])
        calls.clear()
        await b._run(f)
    asyncio.run(go())
    assert any("fork of an earlier run" in c for c in calls if isinstance(c, str))


def test_api_replay_fork_git(tmp_path):
    b = mock_bus(tmp_path)
    t = asyncio.run(b.submit("Build fib"))
    with TestClient(create_app(b)) as c:
        ev = c.get(f"/api/tasks/{t.id}/events").json()
        assert ev[0]["kind"] == "task"
        assert c.get("/api/tasks/task_nope/events").status_code == 404
        g = c.get(f"/api/git/{t.id}/log").json()
        assert g["enabled"] and g["commits"]
        assert c.get("/api/config").json()["git"]["enabled"] is True
        r = c.post(f"/api/git/{t.id}/push", json={})
        assert r.status_code == 400 and "remote" in r.json()["detail"]
        bare = tmp_path / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        r = c.post(f"/api/git/{t.id}/push", json={"remote_url": str(bare)})
        assert r.status_code == 200 and r.json()["branch"] == "main"
        assert c.post(f"/api/tasks/{t.id}/fork", json={"event_id": "evt_x"}).status_code == 404
        assert c.post(f"/api/tasks/{t.id}/fork", json={"event_id": ev[2]["id"], "agent": "ghost"}).status_code == 404
        nt = c.post(f"/api/tasks/{t.id}/fork", json={"event_id": ev[2]["id"], "prompt": "  "}).json()
        assert nt["forked_from"]["task_id"] == t.id and nt["prompt"] == t.prompt and "context" not in nt
        for _ in range(200):
            if b.tasks[nt["id"]].status == "done":
                break
            time.sleep(0.02)
        assert b.tasks[nt["id"]].status == "done"
