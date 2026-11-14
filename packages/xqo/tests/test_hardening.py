"""Failure paths, loop/depth protection, persistence and web API (offline)."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from xqo import bus as busmod
from xqo.agent import Agent
from xqo.artifacts import ArtifactStore
from xqo.bus import HandoverError, MessageBus
from xqo.config import build
from xqo.models import Task
from xqo.web import create_app

ROOT = Path(__file__).resolve().parent.parent


class ScriptLLM:
    """Per-agent scripted responses: a list of callables(messages) -> response."""

    def __init__(self, scripts):
        self.scripts = scripts

    async def create(self, *, agent, messages, **_):
        turn = sum(1 for m in messages if m["role"] == "assistant")
        return self.scripts[agent](turn, messages)


def text(t):
    return NS(content=[NS(type="text", text=t)], stop_reason="end_turn", usage=None, model="m")


def tool(name, **inp):
    return NS(content=[NS(type="tool_use", id=f"t_{name}", name=name, input=inp)],
              stop_reason="tool_use", usage=None, model="m")


def make_bus(tmp_path, agents, scripts):
    store = ArtifactStore(tmp_path / "art")
    b = MessageBus(tmp_path / "ev.jsonl")
    b.artifacts = store
    llm = ScriptLLM(scripts)
    for name, skills in agents.items():
        b.register(Agent(name, skills, "sys", llm, store))
    return b


def last_tool_result(messages):
    return messages[-1]["content"][0]


def test_no_offer_returns_tool_error(tmp_path):
    seen = {}

    def lead(turn, msgs):
        if turn == 0:
            return tool("handover_task", skill="cooking", task="bake")
        seen["r"] = last_tool_result(msgs)
        return text("gave up")

    b = make_bus(tmp_path, {"lead": ["planning"], "dev": ["python"]}, {"lead": lead})
    t = asyncio.run(b.submit("x"))
    assert t.status == "done" and seen["r"]["is_error"] and "no agent offered" in seen["r"]["content"]


def test_failover_to_next_offer(tmp_path):
    def lead(turn, msgs):
        return tool("handover_task", skill="python", task="code") if turn == 0 else text(last_tool_result(msgs)["content"])

    def broken(turn, msgs):
        raise RuntimeError("boom")

    b = make_bus(tmp_path, {"lead": ["planning"], "a": ["python"], "b": ["python"]},
                 {"lead": lead, "a": broken, "b": lambda *_: text("ok from b")})
    t = asyncio.run(b.submit("x"))
    assert t.result == "ok from b"
    assert [e["recipient"] for e in b.history if e["kind"] == "handover"] == ["a", "b"]


def test_loop_and_depth_protection(tmp_path, monkeypatch):
    b = make_bus(tmp_path, {"a": ["x"], "b": ["y"]}, {})
    parent = Task(prompt="p", assignee="a", chain=["a", "b"])
    with pytest.raises(HandoverError):
        asyncio.run(b.direct(parent, "b", "hi"))
    with pytest.raises(HandoverError):
        asyncio.run(b.direct(parent, "zzz", "hi"))
    monkeypatch.setattr(busmod, "MAX_DEPTH", 1)
    deep = Task(prompt="p", assignee="a", chain=["a"], depth=1)
    with pytest.raises(HandoverError, match="depth"):
        asyncio.run(b.direct(deep, "b", "hi"))


def test_task_timeout(tmp_path, monkeypatch):
    async def slow_create(**_):
        await asyncio.sleep(5)

    monkeypatch.setattr(busmod, "TASK_TIMEOUT", 0.05)
    b = make_bus(tmp_path, {"a": ["x"]}, {})
    b.agents["a"].llm = NS(create=slow_create)
    t = asyncio.run(b.submit("x"))
    assert t.status == "error" and "Timeout" in t.result


def test_history_restored_after_restart(tmp_path):
    b = build(ROOT / "agents.json", tmp_path / "art", str(tmp_path / "ev.jsonl"), mock=True)
    t = asyncio.run(b.submit("Build fib"))
    b2 = build(ROOT / "agents.json", tmp_path / "art", str(tmp_path / "ev.jsonl"), mock=True)
    assert len(b2.history) == len(b.history)
    assert b2.tasks[t.id].status == "done" and b2.tasks[t.id].result == t.result
    assert len(b2.tree(t.id)["children"]) == 2


def test_web_api(tmp_path):
    b = build(ROOT / "agents.json", tmp_path / "art", str(tmp_path / "ev.jsonl"), mock=True)
    t = asyncio.run(b.submit("Build fib"))
    c = TestClient(create_app(b))
    assert c.get("/").status_code == 200
    assert {a["name"] for a in c.get("/api/agents").json()} >= {"planner", "coder"}
    tree = c.get(f"/api/tasks/{t.id}").json()
    assert tree["status"] == "done" and tree["children"]
    arts = c.get("/api/artifacts", params={"task_id": t.id}).json()
    assert arts and c.get(f"/api/artifacts/file/{arts[0]['path']}").status_code == 200
    assert c.get("/api/artifacts/file/../ev.jsonl").status_code == 404
    assert c.get("/api/artifacts/file/%2e%2e/ev.jsonl").status_code == 404
    z = c.get(f"/api/artifacts/zip/{t.id}")
    assert z.headers["content-type"] == "application/zip" and len(z.content) > 100
    assert c.post("/api/tasks", json={"prompt": ""}).status_code == 422
    assert c.post("/api/tasks", json={"prompt": "x", "agent": "nobody"}).status_code == 404
    assert c.get("/api/tasks/nope").status_code == 404


def test_task_naming(tmp_path):
    import re as _re

    from xqo.models import short_title

    b = build(ROOT / "agents.json", tmp_path / "art", str(tmp_path / "ev.jsonl"), mock=True)
    t = asyncio.run(b.submit("Build a fibonacci function\nwith tests and docs"))
    assert _re.fullmatch(r"task_\d{10}", t.id)
    assert t.name == f"{t.id} : Build a fibonacci function"
    kids = [c for c in b.tasks.values() if c.parent_id == t.id]
    assert {c.title for c in kids} == {"Implement fib in Python", "Review fib implementation"}
    assert len({c.id for c in b.tasks.values()}) == len(b.tasks)  # same-minute ids get -2, -3 ...
    assert all(_re.fullmatch(r"task_\d{10}(-\d+)?", c.id) for c in b.tasks.values())
    ev = next(e for e in b.history if e["kind"] == "handover")
    assert ev["task_name"].endswith(" : Implement fib in Python") and ev["root_task_name"] == t.name
    assert b.artifacts.task_meta(t.id)["name"] == t.name
    b2 = build(ROOT / "agents.json", tmp_path / "art", str(tmp_path / "ev.jsonl"), mock=True)
    assert b2.tasks[t.id].name == t.name  # titles survive restart
    t2 = asyncio.run(b2.submit("again"))
    assert t2.id != t.id
    assert short_title("x" * 100).endswith("…") and len(short_title("word " * 40)) <= 61


def test_model_written_title_with_fallback(tmp_path):
    b = build(ROOT / "agents.json", tmp_path / "art", str(tmp_path / "ev.jsonl"), mock=True)

    async def good(prompt):
        return "Fibonacci helper.\nextra line"

    async def bad(prompt):
        raise RuntimeError("api down")

    b.titler = good
    assert asyncio.run(b.submit("long prompt ...")).title == "Fibonacci helper"
    b.titler = bad
    assert asyncio.run(b.submit("Make a CSV parser")).title == "Make a CSV parser"


def test_find_placeholders():
    fp = busmod.find_placeholders
    assert fp("Review:\n---\nCODE START\n{content}\nCODE END") == ["{content}"]
    assert fp("Please check {code} for bugs") == ["{code}"]
    assert fp("x\n<your code>\n") == ["<your code>"]
    assert fp("see [paste the file here]") == ["[paste the file here]"]
    assert fp('Fix the f"{x}" call and the dict {"a": 1}') == []
    assert fp('```python\nprint(f"{content}")\n```') == []  # fenced code may hold real braces
    assert fp("x\n<div>\n") == []


def test_delegation_rejects_placeholders_and_references_files(tmp_path):
    seen = {}

    def lead(turn, msgs):
        if turn == 0:
            return tool("write_file", path="src/app.py", content="print(eval(expr))\n")
        if turn == 1:
            return tool("ask_agent", agent="rev", title="Review", message="Review this:\nCODE START\n{content}\nCODE END")
        if turn == 2:
            seen["placeholder"] = last_tool_result(msgs)
            return tool("ask_agent", agent="rev", title="Review", message="Review it.", files=["src/nope.py"])
        if turn == 3:
            seen["missing"] = last_tool_result(msgs)
            return tool("ask_agent", agent="rev", title="Review", message="Review the code below.", files=["src/app.py"])
        return text(last_tool_result(msgs)["content"])

    def rev(turn, msgs):
        seen["prompt"] = msgs[0]["content"]
        return text("looks fine")

    b = make_bus(tmp_path, {"lead": ["planning"], "rev": ["code review"]}, {"lead": lead, "rev": rev})
    t = asyncio.run(b.submit("review it"))
    assert seen["placeholder"]["is_error"] and "{content}" in seen["placeholder"]["content"]
    assert seen["missing"]["is_error"] and "src/nope.py" in seen["missing"]["content"]
    assert "src/app.py" in seen["missing"]["content"]  # lists what is available
    assert "Review the code below." in seen["prompt"] and "- src/app.py (18 chars)" in seen["prompt"]
    assert "print(eval(expr))" not in seen["prompt"]  # a reference: the receiver reads the file itself
    assert t.result.startswith("looks fine")
    direct = [e for e in b.history if e["kind"] == "direct"]
    assert len(direct) == 1 and direct[0]["content"] == "Review the code below."
    assert direct[0]["meta"]["files"] == ["src/app.py"]
