"""Offline end-to-end test: broadcast handover, direct message, artifact extraction. Run: python -m pytest tests"""
import asyncio
from pathlib import Path

from xqo.artifacts import extract_fenced
from xqo.config import build

ROOT = Path(__file__).resolve().parent.parent


def test_extract_fenced():
    text = "a\n```python\nx = 1\n```\nb\n~~~json\n{}\n~~~\n"
    assert extract_fenced(text) == [("python", "x = 1\n"), ("json", "{}\n")]


def test_flow(tmp_path):
    bus = build(ROOT / "agents.json", tmp_path / "art", str(tmp_path / "ev.jsonl"), mock=True)
    task = asyncio.run(bus.submit("Build fib"))
    kinds = [e["kind"] for e in bus.history]
    assert task.status == "done"
    for k in ("broadcast", "offer", "handover", "result", "direct", "reply", "artifact"):
        assert k in kinds, k
    arts = bus.artifacts.list(task.id)
    assert {a["agent"] for a in arts if a.get("action") != "report"} == {"planner", "coder", "reviewer"}
    assert all(a["root_task_id"] == task.id for a in arts)
