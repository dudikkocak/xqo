""""No limits" also lifts the task time limit and department budgets, answers open questions with 'extend',
and gives a fresh period / budget when the hour is over."""
import asyncio
import json
import shutil
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from xqo import bus as busmod
from xqo.agent import Agent
from xqo.artifacts import ArtifactStore
from xqo.bus import MessageBus
from xqo.models import Task

ROOT = Path(__file__).resolve().parent.parent


def text(t):
    return NS(content=[NS(type="text", text=t)], stop_reason="end_turn", usage=None, model="m")


def make(tmp_path):
    store = ArtifactStore(tmp_path / "art")
    b = MessageBus(tmp_path / "ev.jsonl")
    b.artifacts = store
    b.attention.timeout = 30
    return b, store


def test_time_limit_is_lifted_and_restarts_after_the_hour(tmp_path, monkeypatch):
    monkeypatch.setattr(busmod, "TASK_TIMEOUT", 0.3)
    b, store = make(tmp_path)

    class Slow:
        async def create(self, **_):
            await asyncio.sleep(0.8)
            return text("finished")

    b.register(Agent("a", ["x"], "sys", Slow(), store))
    b.limits.enable()
    t = asyncio.run(b.submit("x"))
    assert t.result == "finished"
    assert not [e for e in b.history if e["kind"] == "attention_requested"]


def test_fresh_time_period_when_the_hour_ends(tmp_path, monkeypatch):
    monkeypatch.setattr(busmod, "TASK_TIMEOUT", 0.4)
    b, store = make(tmp_path)

    class Slow:
        async def create(self, **_):
            await asyncio.sleep(0.9)
            return text("finished")

    b.register(Agent("a", ["x"], "sys", Slow(), store))
    b.limits.enable()

    async def go():
        job = asyncio.create_task(b.submit("x"))
        await asyncio.sleep(0.6)  # past the 0.4 s limit while the override is on
        b.limits.disable()  # the hour is over: a fresh 0.4 s period starts now, the task ends within it
        return await job

    t = asyncio.run(go())
    assert t.result == "finished"
    assert not [e for e in b.history if e["kind"] == "attention_requested"]


def test_open_questions_are_extended_when_turned_on(tmp_path, monkeypatch):
    monkeypatch.setattr(busmod, "TASK_TIMEOUT", 0.2)
    b, store = make(tmp_path)

    class Slow:
        async def create(self, **_):
            await asyncio.sleep(0.6)
            return text("finished")

    b.register(Agent("a", ["x"], "sys", Slow(), store))

    async def go():
        job = asyncio.create_task(b.submit("x"))
        while not b.attention.pending():
            await asyncio.sleep(0.01)
        b.limits.enable()
        assert b.lift_pending()
        return await job

    t = asyncio.run(go())
    assert t.result == "finished"
    res = [e for e in b.history if e["kind"] == "attention_resolved"]
    assert res and res[0]["meta"]["answer"] == "extend" and res[0]["meta"]["resolved_by"] == "no_limits"


def test_scheduled_runs_keep_limits_unless_they_honor_no_limits(tmp_path):
    b, _ = make(tmp_path)
    b.limits.enable()
    root = Task("r", "x", "a", "user", id="r1")
    root.root_id = root.id
    b.tasks[root.id] = root
    assert b.limits_lifted(root)
    root.limits = {"honor_no_limits": False}
    assert not b.limits_lifted(root)
    root.limits = {"honor_no_limits": True}
    assert b.limits_lifted(root)


# ------------------------------------------------------------------ department budgets
def budget_bus(tmp_path):
    from xqo.config import build
    shutil.copy(ROOT / "agents.json", tmp_path / "agents.json")
    shutil.copytree(ROOT / "departments", tmp_path / "departments")
    for p in (tmp_path / "departments").glob("*.json"):
        d = json.loads(p.read_text())
        if d["name"] == "engineering":
            d["budget"] = {"max_tokens": 1}
        p.write_text(json.dumps(d))
    return build(tmp_path / "agents.json", tmp_path / "artifacts", str(tmp_path / "e.jsonl"), mock=True)


@pytest.mark.skipif(not (ROOT / "departments").is_dir(), reason="no departments in this build")
def test_budget_is_lifted(tmp_path):
    b = budget_bus(tmp_path)
    b.limits.enable()
    t = asyncio.run(b.submit("Build fib"))
    assert not [e for e in b.history if e["kind"] == "attention_requested" and e["meta"]["kind"] == "budget"]
    assert not [e for e in b.history if e["kind"] == "error" and "budget exhausted" in e["content"]]
    info = [e for e in b.history if e["kind"] == "limits" and e["meta"].get("no_limits")]
    assert info and "engineering" in info[0]["content"]
    assert any(e["sender"] == "coder" and e["kind"] == "done" for e in b.history)
    assert t.status == "done"


@pytest.mark.skipif(not (ROOT / "departments").is_dir(), reason="no departments in this build")
def test_fresh_budget_after_the_hour(tmp_path):
    from xqo.departments import Budget
    bus = NS(departments={"eng": NS(budget={"max_tokens": 100, "max_cost_usd": 1.0})}, emit=lambda e: None,
             lifted=True)
    bus.limits_lifted = lambda task: bus.lifted
    bg = Budget(bus)
    task = NS(id="t", root_id="r", parent_id=None)
    bg.add("r", "eng", "claude-opus-5-5", {"input_tokens": 250})
    asyncio.run(bg.check(task, "coder", "eng"))  # over, but lifted: no question
    assert ("r", "eng") in bg.lifted
    bus.lifted = False
    asyncio.run(bg.check(task, "coder", "eng"))  # hour over: a fresh budget from 250 tokens
    assert bg.caps("r", "eng")["max_tokens"] == 350 and bg.over("r", "eng") is None
    bg.add("r", "eng", "claude-opus-5-5", {"input_tokens": 100})
    assert "350 tokens of 350" in bg.over("r", "eng")
