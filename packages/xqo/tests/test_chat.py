"""Talking to agents during a task (Ask / Steer / urgent), pause and resume, reopening, failing tests."""
import asyncio
import os
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from xqo.config import build
from xqo.web import create_app

ROOT = Path(__file__).resolve().parent.parent


def make(tmp_path, attention_timeout=None):
    b = build(ROOT / "agents.json", tmp_path / "art", str(tmp_path / "logs" / "ev.jsonl"), mock=True)
    if attention_timeout is not None:
        b.attention.timeout = attention_timeout
    return b


async def until(cond, timeout=10):
    t0 = time.monotonic()
    while not cond():
        if time.monotonic() - t0 > timeout:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.02)


def hold(b, agent):
    """Make one agent's model calls wait until the returned event is set."""
    gate = asyncio.Event()
    orig = b.agents[agent].llm.create
    calls = {"n": 0}

    async def slow(**kw):
        if kw.get("agent") == agent:
            calls["n"] += 1
            await gate.wait()
        return await orig(**kw)

    b.agents[agent].llm.create = slow
    return gate, calls


def kinds(b, root, *names):
    return [e for e in b.task_events(root) if e["kind"] in names]


def root_of(b):
    return next(t for t in b.tasks.values() if t.parent_id is None)


def busy(b, agent):
    return any(t.assignee == agent and t.status == "running" for t in b.tasks.values())


# ------------------------------------------------------------------ ask / steer
def test_ask_waits_for_next_turn_and_gets_a_reply(tmp_path):
    b = make(tmp_path)

    async def go():
        gate, _ = hold(b, "reviewer")
        job = asyncio.create_task(b.submit("Build fib"))
        await until(lambda: busy(b, "reviewer"))
        root = root_of(b)
        r = await b.chat.send(root.id, "How is it going?", "ask")
        assert r["delivered_to"] == [root.id] and b.chat.inbox[root.id]
        q = await b.chat.send(root.id, "Any risk in the review?", "ask", to="reviewer")
        rev = next(t for t in b.tasks.values() if t.assignee == "reviewer")
        assert q["delivered_to"] == [rev.id]
        gate.set()
        return await job

    t = asyncio.run(go())
    assert t.status == "done"
    msgs = kinds(b, t.id, "user_message")
    assert [m["meta"]["kind"] for m in msgs] == ["ask", "ask"] and msgs[1]["recipient"] == "reviewer"
    replies = kinds(b, t.id, "agent_reply")
    assert {r["sender"] for r in replies} == {"planner", "reviewer"}
    assert "How is it going?" in next(r for r in replies if r["sender"] == "planner")["content"]


def test_urgent_ask_answers_now_and_steer_reaches_everyone(tmp_path):
    b = make(tmp_path)

    async def go():
        gate, _ = hold(b, "reviewer")
        job = asyncio.create_task(b.submit("Build fib"))
        await until(lambda: busy(b, "reviewer"))
        root = root_of(b)
        await b.chat.send(root.id, "Quick: which algorithm?", "ask", urgent=True)
        await until(lambda: kinds(b, root.id, "agent_reply"))  # answered while the reviewer still works
        assert busy(b, "reviewer")
        first = kinds(b, root.id, "agent_reply")[0]
        assert first["sender"] == "planner" and first["meta"].get("urgent")
        s = await b.chat.send(root.id, "Use an iterative version only", "steer")
        rev = next(t for t in b.tasks.values() if t.assignee == "reviewer")
        assert set(s["delivered_to"]) == {root.id, rev.id}
        gate.set()
        return await job

    t = asyncio.run(go())
    assert t.status == "done"
    acks = [r for r in kinds(b, t.id, "agent_reply") if "adjusting the plan" in r["content"]]
    assert {r["sender"] for r in acks} == {"planner", "reviewer"}


def test_chat_answers_a_pending_question(tmp_path):
    b = make(tmp_path)

    async def go():
        job = asyncio.create_task(b.submit("Please confirm, then build fib"))
        await until(lambda: b.attention.pending())
        root = root_of(b)
        r = await b.chat.send(root.id, "recursive", "ask")
        assert r["answered_attention"]
        return await job

    t = asyncio.run(go())
    att = next(iter(b.attention.items.values()))
    assert att.status == "answered" and att.answer == "recursive" and "chat" in att.resolved_by


def test_bad_messages(tmp_path):
    b = make(tmp_path)
    t = asyncio.run(b.submit("Build fib"))

    async def go():
        with pytest.raises(KeyError):
            await b.chat.send(t.id, "hi", "ask", to="nobody")
        with pytest.raises(ValueError):
            await b.chat.send(t.id, "  ", "ask")
        with pytest.raises(ValueError):
            await b.chat.send(t.id, "hi", "shout")
    asyncio.run(go())


# ------------------------------------------------------------------ pause / resume
def test_pause_holds_next_calls_and_freezes_timers(tmp_path):
    b = make(tmp_path, attention_timeout=0.6)

    async def go():
        job = asyncio.create_task(b.submit("Please confirm, then build fib"))
        await until(lambda: b.attention.pending())
        root = root_of(b)
        b.chat.pause(root.id)
        assert root.status == "paused"
        await asyncio.sleep(1.2)  # past the 0.6s auto-confirm: frozen while paused
        att = b.attention.pending()[0]
        assert att.status == "pending" and att.frozen
        calls = b.agents["planner"].usage["calls"]
        b.chat.resume(root.id)
        t = await job
        assert b.agents["planner"].usage["calls"] > calls
        return t

    t = asyncio.run(go())
    assert t.status == "done"
    att = next(iter(b.attention.items.values()))
    assert att.status == "auto_confirmed"
    ks = [e["kind"] for e in b.task_events(t.id)]
    assert ks.index("task_paused") < ks.index("task_resumed")


def test_pause_waits_for_the_call_in_progress(tmp_path):
    b = make(tmp_path)

    async def go():
        gate, calls = hold(b, "reviewer")
        job = asyncio.create_task(b.submit("Build fib"))
        await until(lambda: busy(b, "reviewer"))
        root = root_of(b)
        b.chat.pause(root.id)
        gate.set()  # the reviewer's call in progress finishes...
        await until(lambda: any(e["kind"] == "done" and e["sender"] == "reviewer" for e in b.history))
        planner_calls = b.agents["planner"].usage["calls"]
        await asyncio.sleep(0.4)  # ...but the planner's next call waits
        assert b.agents["planner"].usage["calls"] == planner_calls and b.agents["planner"].waiting == 1
        assert b.agents["planner"].describe()["paused"] == 1
        b.chat.resume(root.id)
        return await job

    t = asyncio.run(go())
    assert t.status == "done"
    note = [e for e in b.history if e["kind"] == "llm" and e["sender"] == "planner"]
    assert note  # it carried on after the resume


@pytest.mark.skipif(os.name == "nt", reason="process groups")
def test_pause_stops_test_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("XQO_SANDBOX", "local")
    b = make(tmp_path)
    from types import SimpleNamespace as NS

    async def go():
        root = await b.create("x", "planner")
        b.tasks[root.id] = root
        b._jobs[root.id] = asyncio.current_task()  # pretend it runs
        child = b._child(root, "tester", "run")
        b.tasks[child.id] = child
        call = NS(id="t1", name="run_code", input={"command": f"{sys.executable} -c 'import time; time.sleep(30)'"})
        run = asyncio.create_task(b.agents["tester"]._tool(child, call))
        await until(lambda: any(e["kind"] == "sandbox" and e["meta"].get("phase") == "start" for e in b.history))
        await asyncio.sleep(0.3)
        t0 = time.monotonic()
        b.chat.pause(root.id)
        r = await run
        assert time.monotonic() - t0 < 5
        return r

    r = asyncio.run(go())
    assert r["is_error"] and "paused" in r["content"]


def test_reopen_finished_task_and_restart(tmp_path):
    b = make(tmp_path)
    t = asyncio.run(b.submit("Build fib"))

    async def go():
        r = await b.chat.send(t.id, "Can you add a docstring?", "steer")
        assert r["reopened"]
        await until(lambda: not b.is_running(t.id) and t.status == "done")
    asyncio.run(go())
    ks = [e["kind"] for e in b.task_events(t.id)]
    assert "task_reopened" in ks and ks.count("done") >= 2
    assert any("docstring" in e["content"] for e in kinds(b, t.id, "agent_reply"))
    assert t.context.startswith("This task ran before")
    assert (b.artifacts.workspace(t.id) / "src" / "fib.py").is_file()
    # a task paused when the server stops is shown as paused, and Resume continues it
    b2 = make(tmp_path)

    async def pause_then_stop():
        job = asyncio.create_task(b2.submit("Please confirm, then build fib"))
        await until(lambda: b2.attention.pending())
        root = next(x for x in b2.tasks.values() if x.parent_id is None and x.status == "needs_attention")
        b2.chat.pause(root.id)
        # the server dies here: keep the log as it is now (stopping the job would log a cancel)
        crashed = b2.log_path.read_text()
        job.cancel()
        return root.id, crashed
    rid, crashed = asyncio.run(pause_then_stop())
    b2.log_path.write_text(crashed)
    b3 = make(tmp_path)
    assert b3.tasks[rid].status == "paused"

    async def resume():
        task, reopened = b3.chat.resume(rid)
        assert reopened
        await until(lambda: b3.tasks[rid].status not in ("pending", "running", "needs_attention", "paused")
                    or b3.attention.pending())
    asyncio.run(resume())


def test_fork_from_a_chat_message(tmp_path):
    b = make(tmp_path)
    t = asyncio.run(b.submit("Build fib"))

    async def go():
        await b.chat.send(t.id, "Explain the result", "ask")
        await until(lambda: not b.is_running(t.id))
        msg = kinds(b, t.id, "user_message")[0]
        return await b.fork(t.id, msg["id"])
    f = asyncio.run(go())
    assert "user_message" in f.context and "Explain the result" in f.context


# ------------------------------------------------------------------ API
def test_api_chat_pause_resume_and_urgent_task(tmp_path):
    b = make(tmp_path)

    async def start():
        job = asyncio.create_task(b.submit("Please confirm, then build fib"))
        await until(lambda: b.attention.pending())
        return job

    with TestClient(create_app(b)) as c:
        job = c.portal.call(start)
        root = root_of(b)
        assert c.post(f"/api/tasks/{root.id}/pause", json={}).json()["status"] == "paused"
        assert c.post(f"/api/tasks/{root.id}/resume").json()["reopened"] is False
        assert c.post(f"/api/tasks/{root.id}/chat", json={"text": "x", "kind": "nope"}).status_code == 422
        assert c.post(f"/api/tasks/{root.id}/chat", json={"text": "x", "to": "ghost"}).status_code == 404
        r = c.post("/api/tasks", json={"prompt": "Build fib", "urgent": True, "pause_task": root.id}).json()
        assert r["paused"] == root.id and b.chat.is_paused(root.id)
        assert c.post(f"/api/tasks/{root.id}/chat", json={"text": "iterative", "kind": "ask"}).json()["answered_attention"]
        assert c.post(f"/api/tasks/{root.id}/resume").status_code == 200
        c.portal.call(lambda: asyncio.wait_for(asyncio.shield(job), 10))
    assert root.status == "done"


# ------------------------------------------------------------------ failing tests
def test_failing_tests_are_flagged_and_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("XQO_SANDBOX", "local")
    b = make(tmp_path, attention_timeout=0.3)
    t = asyncio.run(b.submit("Build fib and run tests, with failing tests"))
    tester = next(x for x in b.tasks.values() if x.assignee == "tester")
    assert tester.tests["runs"] == 3 and tester.tests["last_exit"] == 1
    att = [a for a in b.attention.items.values() if a.kind == "tests_failing"]
    assert att and att[0].status == "auto_confirmed" and att[0].answer == "stop"
    res = next(e for e in kinds(b, t.id, "result") if e["sender"] == "tester")
    assert res["meta"]["tests"]["failed"] == 3
    assert t.tests["last_exit"] == 1  # the root shows the tree's last test result
    done = [e for e in kinds(b, t.id, "done") if e["task_id"] == t.id][-1]
    assert done["meta"]["tests"]["last_exit"] == 1


def test_passing_tests_marked(tmp_path, monkeypatch):
    monkeypatch.setenv("XQO_SANDBOX", "local")
    b = make(tmp_path)
    t = asyncio.run(b.submit("Build fib and run tests"))
    assert t.tests["runs"] == 1 and t.tests["last_exit"] == 0
    run = next(r for r in b.artifacts.list(t.id) if r["action"] == "run")
    assert run


# ------------------------------------------------------------------ departments
def test_message_to_a_department_goes_to_its_head(tmp_path):
    import json
    import shutil
    shutil.copy(ROOT / "agents.json", tmp_path / "agents.json")
    shutil.copytree(ROOT / "departments", tmp_path / "departments")
    b = build(tmp_path / "agents.json", tmp_path / "art", str(tmp_path / "logs" / "ev.jsonl"), mock=True)

    async def go():
        gate, _ = hold(b, "coder")
        job = asyncio.create_task(b.submit("Build fib"))
        await until(lambda: busy(b, "coder") and busy(b, "eng_lead"))
        root = root_of(b)
        r = await b.chat.send(root.id, "Status of the engineering work?", "ask", to="@engineering")
        lead = next(t for t in b.tasks.values() if t.assignee == "eng_lead")
        assert r["delivered_to"] == [lead.id]
        r = await b.chat.send(root.id, "Write docs too", "ask", to="marketing")  # not at work: its head answers
        assert r["delivered_to"] == [] and r["answered_by"] == "mkt_lead"
        await until(lambda: any(e["sender"] == "mkt_lead" for e in kinds(b, root.id, "agent_reply")))
        with pytest.raises(KeyError, match="engineering"):
            await b.chat.send(root.id, "x", "ask", to="nowhere")
        gate.set()
        return await job

    t = asyncio.run(go())
    replies = {e["sender"] for e in kinds(b, t.id, "agent_reply")}
    assert {"eng_lead", "mkt_lead"} <= replies and "planner" not in replies


def dept_bus(tmp_path):
    import shutil
    shutil.copy(ROOT / "agents.json", tmp_path / "agents.json")
    shutil.copytree(ROOT / "departments", tmp_path / "departments")
    return build(tmp_path / "agents.json", tmp_path / "art", str(tmp_path / "logs" / "ev.jsonl"), mock=True)


def test_ask_and_steer_to_an_idle_agent_do_not_stop_at_the_planner(tmp_path):
    """Regression: Ask/Steer with a recipient picked in the dashboard always reached the planner."""
    b = dept_bus(tmp_path)

    async def go():
        gate, _ = hold(b, "planner")
        job = asyncio.create_task(b.submit("Build fib"))
        await until(lambda: busy(b, "planner") and b.chat.convos)  # only the planner is at work
        root = root_of(b)
        r = await b.chat.send(root.id, "Which framework would you use?", "ask", to="@engineering")
        assert r["answered_by"] == "eng_lead" and root.id not in b.chat.inbox  # nothing queued for the planner
        await until(lambda: kinds(b, root.id, "agent_reply"))
        reply = kinds(b, root.id, "agent_reply")[0]
        assert reply["sender"] == "eng_lead" and reply["meta"]["in_reply_to"] == r["id"]
        msg = kinds(b, root.id, "user_message")[-1]
        assert msg["recipient"] == "eng_lead" and msg["meta"]["to"] == "engineering"
        s = await b.chat.send(root.id, "Use FastAPI instead", "steer", to="@engineering")
        assert s["redirected"] and s["to_agent"] == "eng_lead"
        queued = [m for m in b.chat.inbox[root.id] if m["kind"] == "steer"]
        from xqo.chat import render
        assert "pass it on to 'eng_lead' with ask_agent" in render(queued[0])
        gate.set()
        return await job

    t = asyncio.run(go())
    assert t.status == "done"


def test_ask_an_agent_about_a_finished_task_does_not_reopen_it(tmp_path):
    b = dept_bus(tmp_path)
    t = asyncio.run(b.submit("Build fib"))

    async def go():
        r = await b.chat.send(t.id, "What did you change?", "ask", to="eng_lead")
        assert r["answered_by"] == "eng_lead" and "reopened" not in r
        await until(lambda: any(e["sender"] == "eng_lead" and e["meta"].get("in_reply_to") == r["id"]
                                for e in kinds(b, t.id, "agent_reply")))
        assert not b.is_running(t.id) and t.status == "done"
        s = await b.chat.send(t.id, "Add a CLI", "steer", to="engineering")  # work: the task reopens
        assert s["reopened"] and s["redirected"]
        from xqo.chat import render
        assert "eng_lead" in render({**s, "kind": "steer"})
        await until(lambda: not b.is_running(t.id))

    asyncio.run(go())
