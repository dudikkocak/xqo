"""Agent reports: final answers and the report tool are saved as Markdown, listed per agent (newest 10)."""
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
        self.fn = fn

    async def create(self, *, messages, **_):
        return self.fn(sum(1 for m in messages if m["role"] == "assistant"), messages)


def make(tmp_path, fn):
    store = ArtifactStore(tmp_path / "art")
    b = MessageBus(tmp_path / "ev.jsonl")
    b.artifacts = store
    b.attention.timeout = 0.2
    b.register(Agent("writer", ["x"], "sys", Script(fn), store))
    return b, store


def test_final_answer_is_saved_as_a_report(tmp_path):
    b, store = make(tmp_path, lambda turn, m: text("# Done\n\n## Summary\nAll good."))
    t = asyncio.run(b.submit("Write the summary"))
    reps = store.reports("writer")
    assert len(reps) == 1 and reps[0]["auto"] and reps[0]["status"] == "done"
    assert reps[0]["path"].startswith(f"{t.id}/writer/reports/writer_") and reps[0]["path"].endswith(".md")
    body = store.resolve(reps[0]["path"]).read_text()
    assert "## Summary\nAll good." in body and "**Agent:** writer" in body
    assert any(r["action"] == "report" for r in store.list(t.id))  # in the artifact history


def test_report_tool(tmp_path):
    def fn(turn, m):
        if turn == 0:
            return tool("report", title="Progress 1", markdown="## Progress\n- step one")
        return text("final")
    b, store = make(tmp_path, fn)
    asyncio.run(b.submit("go"))
    reps = store.reports("writer")
    assert [r["title"] for r in reps][1] == "Progress 1" and not reps[1]["auto"]
    assert store.resolve(reps[1]["path"]).read_text() == "## Progress\n- step one"
    assert reps[1]["path"].split("/")[-1].startswith("writer_") and "progress-1" in reps[1]["path"]
    assert any(e["kind"] == "artifact" and e["meta"].get("action") == "report" for e in b.history)


def test_failed_task_gets_a_short_report(tmp_path, monkeypatch):
    monkeypatch.setattr(busmod, "TASK_TIMEOUT", 0.1)

    class Slow:
        async def create(self, **_):
            await asyncio.sleep(1)
            return text("late")

    store = ArtifactStore(tmp_path / "art")
    b = MessageBus(tmp_path / "ev.jsonl")
    b.artifacts = store
    b.attention.timeout = 0.05  # the time-limit question auto-answers 'stop'
    b.register(Agent("writer", ["x"], "sys", Slow(), store))
    t = asyncio.run(b.submit("go"))
    assert t.status == "error"
    rep = store.reports("writer")[0]
    assert rep["status"] == "error"
    body = store.resolve(rep["path"]).read_text()
    assert "## What happened" in body and "## Files touched" in body


def test_only_the_last_ten_and_api(tmp_path):
    b = build(ROOT / "agents.json", tmp_path / "art", str(tmp_path / "ev.jsonl"), mock=True)
    name = next(iter(b.agents))
    for i in range(12):
        b.artifacts.save_report("task_1", "task_1", name, f"R{i}", f"# R{i}")
    with TestClient(create_app(b)) as c:
        rows = c.get(f"/api/agents/{name}/reports").json()
        assert len(rows) == 10 and rows[0]["title"] == "R11"
        md = c.get("/api/reports/" + rows[0]["path"]).json()["markdown"]
        assert md == "# R11"
        assert c.get("/api/reports/../agents.json").status_code == 404
        assert c.get("/api/agents/nobody/reports").status_code == 404
