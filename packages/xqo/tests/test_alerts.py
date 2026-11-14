"""Attention alerts: questions, errors, finished root tasks and auto-answered questions stay until dealt with."""
import asyncio
from types import SimpleNamespace as NS

from pathlib import Path

from fastapi.testclient import TestClient

from xqo.agent import Agent
from xqo.artifacts import ArtifactStore
from xqo.bus import MessageBus
from xqo.config import build
from xqo.models import Event, Task
from xqo.web import create_app

ROOT = Path(__file__).resolve().parent.parent


def text(t):
    return NS(content=[NS(type="text", text=t)], stop_reason="end_turn", usage=None, model="m")


def tool(name, **inp):
    return NS(content=[NS(type="tool_use", id=f"t_{name}", name=name, input=inp)],
              stop_reason="tool_use", usage=None, model="m")


class Script:
    def __init__(self, fn):
        self.fn = fn

    async def create(self, *, messages, **_):
        return self.fn(sum(1 for m in messages if m["role"] == "assistant"), messages)


def make(tmp_path, fn, timeout=0.2):
    store = ArtifactStore(tmp_path / "art")
    b = MessageBus(tmp_path / "ev.jsonl")
    b.artifacts = store
    b.attention.timeout = timeout
    b.register(Agent("a", ["x"], "sys", Script(fn), store))
    return b


def asker(turn, msgs):
    if turn == 0:
        return tool("ask_user", question="Deploy?", reason="approval", options=["yes", "no"], default="no")
    return text("ok")


def test_finished_root_task_alerts_until_dismissed(tmp_path):
    b = make(tmp_path, lambda turn, m: text("done!"))
    t = asyncio.run(b.submit("go"))
    al = b.attention.alerts_pending()
    assert [a.kind for a in al] == ["done"] and al[0].agent == "a" and al[0].root_id == t.id
    assert b.attention.by_agent() == {"a": {"count": 1, "kinds": ["done"]}}
    assert b.attention.ack() == [al[0].id] and not b.attention.alerts_pending()
    # survives a restart: the dismissal is logged too
    assert not MessageBus(tmp_path / "ev.jsonl").attention.alerts_pending()


def test_alerts_survive_a_restart(tmp_path):
    b = make(tmp_path, lambda turn, m: text("done!"))
    asyncio.run(b.submit("go"))
    assert [a.kind for a in MessageBus(tmp_path / "ev.jsonl").attention.alerts_pending()] == ["done"]


def test_auto_confirmed_question_keeps_alerting(tmp_path):
    b = make(tmp_path, asker)
    asyncio.run(b.submit("go"))
    kinds = sorted(a.kind for a in b.attention.alerts_pending())
    assert kinds == ["auto_confirmed", "done"]
    auto = next(a for a in b.attention.alerts_pending() if a.kind == "auto_confirmed")
    assert "'no' was taken" in auto.text


def test_answered_question_clears_its_alert(tmp_path):
    b = make(tmp_path, asker, timeout=30)

    async def go():
        job = asyncio.create_task(b.submit("go"))
        while not b.attention.pending():
            await asyncio.sleep(0.01)
        assert [a.kind for a in b.attention.alerts_pending()] == ["attention"]
        assert b.attention.ack() == []  # an open question stays until it is answered
        b.attention.resolve(b.attention.pending()[0].id, "yes")
        return await job

    asyncio.run(go())
    assert [a.kind for a in b.attention.alerts_pending()] == ["done"]


def test_errors_alert_and_subtasks_do_not_alert_when_done(tmp_path):
    b = MessageBus(tmp_path / "ev.jsonl")
    b.tasks["t1"] = Task("p", "a", id="t1", root_id="t1")
    b.tasks["t2"] = Task("p", "b", parent_id="t1", id="t2", root_id="t1")
    b.emit(Event("error", "a", "user", "t1", None, "Boom"))
    b.emit(Event("done", "b", "a", "t2", "t1", "sub done", {"status": "done"}))  # a sub-task: no alert
    al = b.attention.alerts_pending()
    assert [a.kind for a in al] == ["error"] and al[0].text == "Boom"


def test_cancelled_tasks_and_unattended_runs_do_not_alert(tmp_path):
    b = MessageBus(tmp_path / "ev.jsonl")
    b.emit(Event("done", "a", "user", "t1", None, "Cancelled", {"status": "cancelled"}))
    assert not b.attention.alerts_pending()
    b.emit(Event("attention_requested", "a", "user", "t1", None, "q?", {"id": "att_1", "timeout": 0, "agent": "a"}))
    assert not b.attention.alerts_pending()


def test_deleting_or_reopening_a_task_clears_its_alerts(tmp_path):
    b = MessageBus(tmp_path / "ev.jsonl")
    b.emit(Event("done", "a", "user", "t1", None, "ok", {"status": "done"}))
    b.emit(Event("error", "a", "user", "t2", None, "bad"))
    b.emit(Event("task_reopened", "user", None, "t1", None, ""))
    b.emit(Event("task_deleted", "user", None, "t2", None, ""))
    assert not b.attention.alerts_pending()


def test_api(tmp_path):
    b = build(ROOT / "agents.json", tmp_path / "art", str(tmp_path / "ev.jsonl"), mock=True)
    name = next(iter(b.agents))
    b.emit(Event("done", name, "user", "t1", None, "ok", {"status": "done"}))
    with TestClient(create_app(b)) as c:
        r = c.get("/api/alerts").json()
        assert [a["kind"] for a in r["items"]] == ["done"] and r["by_agent"][name]["count"] == 1
        assert next(a for a in c.get("/api/agents").json() if a["name"] == name)["alerts"]["count"] == 1
        assert c.post("/api/alerts/ack", json={"agent": name}).json()["acked"] == [r["items"][0]["id"]]
        assert c.get("/api/alerts").json()["items"] == []
