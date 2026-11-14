"""Attention flags: ask_user, auto-confirm, automatic triggers, API, persistence."""
import asyncio
from pathlib import Path
from types import SimpleNamespace as NS

from fastapi.testclient import TestClient

from xqo import bus as busmod
from xqo.agent import Agent
from xqo.artifacts import ArtifactStore
from xqo.bus import MessageBus
from xqo.config import build
from xqo.web import create_app

ROOT = Path(__file__).resolve().parent.parent


def text(t):
    return NS(content=[NS(type="text", text=t)], stop_reason="end_turn", usage=None, model="m")


def tool(name, **inp):
    return NS(content=[NS(type="tool_use", id=f"t_{name}", name=name, input=inp)],
              stop_reason="tool_use", usage=None, model="m")


class Script:
    def __init__(self, fn):
        self.fn, self.seen = fn, []

    async def create(self, *, messages, **_):
        self.seen.append(messages[-1]["content"])
        return self.fn(sum(1 for m in messages if m["role"] == "assistant"), messages)


def make(tmp_path, scripts, timeout=0.2):
    store = ArtifactStore(tmp_path / "art")
    b = MessageBus(tmp_path / "ev.jsonl")
    b.artifacts = store
    b.attention.timeout = timeout
    for name, (skills, llm) in scripts.items():
        b.register(Agent(name, skills, "sys", llm, store))
    return b


def asker(turn, msgs):
    if turn == 0:
        return tool("ask_user", question="Deploy?", reason="approval", options=["yes", "no"], default="no")
    return text(msgs[-1]["content"][0]["content"])


def test_ask_user_auto_confirms_default(tmp_path):
    b = make(tmp_path, {"a": (["x"], Script(asker))})
    t = asyncio.run(b.submit("go"))
    assert "auto-confirmed default: no" in t.result
    kinds = [e["kind"] for e in b.history]
    assert "attention_requested" in kinds and "attention_resolved" in kinds
    res = next(e for e in b.history if e["kind"] == "attention_resolved")
    assert res["meta"]["status"] == "auto_confirmed" and res["meta"]["resolved_by"] == "timeout"


def test_user_reply_resumes_agent_and_status(tmp_path):
    b = make(tmp_path, {"a": (["x"], Script(asker))}, timeout=30)

    async def scenario():
        job = asyncio.create_task(b.submit("go"))
        while not b.attention.pending():
            await asyncio.sleep(0.01)
        att = b.attention.pending()[0]
        task = b.tasks[att.task_id]
        assert task.status == "needs_attention" and att.kind == "approval"
        b.attention.resolve(att.id, "yes")
        return await job

    t = asyncio.run(scenario())
    assert t.result == "User replied: yes" and t.status == "done"


def test_no_offer_user_assigns_agent(tmp_path):
    def lead(turn, msgs):
        return tool("handover_task", skill="cooking", task="bake", title="Bake") if turn == 0 \
            else text(msgs[-1]["content"][0]["content"])

    b = make(tmp_path, {"lead": (["planning"], Script(lead)), "dev": (["python"], Script(lambda *_: text("baked")))},
             timeout=30)

    async def scenario():
        job = asyncio.create_task(b.submit("x"))
        while not b.attention.pending():
            await asyncio.sleep(0.01)
        att = b.attention.pending()[0]
        assert att.kind == "no_offer" and "dev" in att.options
        b.attention.resolve(att.id, "dev")
        return await job

    assert asyncio.run(scenario()).result == "baked"


def test_time_limit_asks_and_extend(tmp_path, monkeypatch):
    monkeypatch.setattr(busmod, "TASK_TIMEOUT", 0.3)

    class Slow:
        async def create(self, **_):
            await asyncio.sleep(0.5)
            return text("finished")

    b = make(tmp_path, {"a": (["x"], Slow())}, timeout=30)

    async def scenario():
        job = asyncio.create_task(b.submit("x"))
        while not b.attention.pending():
            await asyncio.sleep(0.01)
        att = b.attention.pending()[0]
        assert att.kind == "timeout" and att.default == "stop"
        b.attention.resolve(att.id, "extend")
        return await job

    assert asyncio.run(scenario()).result == "finished"


def test_waiting_on_user_pauses_time_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(busmod, "TASK_TIMEOUT", 0.4)
    b = make(tmp_path, {"a": (["x"], Script(asker))}, timeout=0.8)  # wait longer than the limit
    t = asyncio.run(b.submit("go"))
    assert t.status == "done" and not [e for e in b.history if e["meta"].get("kind") == "timeout"]


def test_api_errors_ask_user_then_retry(tmp_path):
    import anthropic
    import httpx

    calls = {"n": 0}

    class Flaky:
        async def create(self, **_):
            calls["n"] += 1
            if calls["n"] == 1:
                raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://x"))
            return text("ok")

    b = make(tmp_path, {"a": (["x"], Flaky())})
    t = asyncio.run(b.submit("x"))
    assert t.result == "ok"
    att = next(e for e in b.history if e["kind"] == "attention_requested")
    assert att["meta"]["kind"] == "retries_exhausted" and att["meta"]["default"] == "retry"


def test_patch_failure_flags_user_after_retries(tmp_path):
    bad = "```python a.py\nx = 1\n```\n```diff\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-nope\n+x = 2\n```"
    b = make(tmp_path, {"a": (["x"], Script(lambda turn, m: text(bad if turn < 3 else "done")))})
    asyncio.run(b.submit("x"))
    kinds = [e["meta"].get("kind") for e in b.history if e["kind"] == "attention_requested"]
    assert "patch_failed" in kinds


def test_api_and_persistence(tmp_path):
    b = build(ROOT / "agents.json", tmp_path / "art", str(tmp_path / "ev.jsonl"), mock=True)
    b.attention.timeout = 30
    c = TestClient(create_app(b))

    async def scenario():
        job = asyncio.create_task(b.submit("Please confirm the approach, then build fib"))
        while not b.attention.pending():
            await asyncio.sleep(0.01)
        return job

    loop = asyncio.new_event_loop()
    job = loop.run_until_complete(scenario())
    pend = c.get("/api/attention", params={"pending": True}).json()
    assert len(pend) == 1 and pend[0]["options"] == ["iterative", "recursive"] and pend[0]["remaining"] > 0
    assert c.get("/api/agents").json()[0]["attention"] == 1
    att_id = pend[0]["id"]
    assert c.post("/api/attention/nope", json={"answer": "x"}).status_code == 404
    assert c.post(f"/api/attention/{att_id}", json={"answer": "recursive"}).json()["status"] == "answered"
    assert c.post(f"/api/attention/{att_id}", json={"answer": "again"}).status_code == 409
    task = loop.run_until_complete(job)
    loop.close()
    assert task.status == "done"
    b2 = build(ROOT / "agents.json", tmp_path / "art", str(tmp_path / "ev.jsonl"), mock=True)
    assert b2.attention.items[att_id].status == "answered" and b2.attention.items[att_id].answer == "recursive"
