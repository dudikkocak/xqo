"""write_paths: the planner writes plans and steps, never code; code fixes are handed to engineering."""
import asyncio
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from xqo.artifacts import ArtifactStore
from xqo.chat import render
from xqo.config import build
from xqo.models import Task

ROOT = Path(__file__).resolve().parent.parent


def make(tmp_path):
    return build(ROOT / "agents.json", tmp_path / "art", str(tmp_path / "logs" / "ev.jsonl"), mock=True)


@pytest.mark.parametrize("path,ok", [
    ("plans/plan.md", True), ("plans/phase1/steps.txt", True), ("docs/arch/overview.md", True),
    ("README.md", True), ("./plans/x.md", True), ("src/app.py", False), ("src/notes.txt", False),
])
def test_planner_write_paths(tmp_path, path, ok):
    b = make(tmp_path)
    assert b.agents["planner"].may_write(path) is ok
    assert b.agents["coder"].may_write(path)  # unset write_paths: any file


def test_fenced_writes_outside_allow_are_kept_as_blocked_snippets(tmp_path):
    store = ArtifactStore(tmp_path / "art")
    allow = lambda p: p.endswith(".md")
    text = ("```python src/app.py\nx = 1\n```\n\n```markdown plans/plan.md\n# Plan\n```\n\n"
            "```diff\n--- a/src/b.py\n+++ b/src/b.py\n@@ -0,0 +1 @@\n+y = 2\n```\n\n"
            "```src/c.py\n<<<<<<< SEARCH\n=======\nz = 3\n>>>>>>> REPLACE\n```\n\n```python\nsnippet = 1\n```")
    recs, errors = store.process_text(text, "t1", "planner", "r1", allow=allow)
    assert not errors
    assert [r.get("blocked") for r in recs if r["action"] == "blocked"] == [["src/app.py"], ["src/b.py"], ["src/c.py"]]
    assert {f["file"] for f in store.list_files("r1")} == {"plans/plan.md"}
    assert [r["action"] for r in recs if r["action"] != "blocked"] == ["create", "snippet"]


def test_planner_tools_refuse_code_files(tmp_path):
    b = make(tmp_path)
    planner = b.agents["planner"]
    task = Task(prompt="p", assignee="planner")

    def call(name, **inp):
        return asyncio.run(planner._tool(task, NS(id="c1", name=name, input=inp)))

    r = call("write_file", path="src/app.py", content="x = 1\n")
    assert r.get("is_error") and "engineering" in r["content"] and "handover_task" in r["content"]
    r = call("patch_file", path="plans/plan.md", diff="--- a/src/app.py\n+++ b/src/app.py\n@@ -0,0 +1 @@\n+x\n")
    assert r.get("is_error")  # the diff's own headers decide the files it changes
    r = call("write_file", path="plans/plan.md", content="# Plan\n1. Ask engineering to fix the bug\n")
    assert not r.get("is_error") and "plans/plan.md" in r["content"]
    assert {f["file"] for f in planner.artifacts.list_files(task.root_id)} == {"plans/plan.md"}


def test_planner_is_told_when_fenced_code_is_refused(tmp_path):
    b = make(tmp_path)
    planner = b.agents["planner"]
    orig, seen = planner.llm.create, []

    async def fake(**kw):
        if kw.get("agent") == "planner":
            seen.append({**kw, "messages": list(kw["messages"])})  # the live list grows later
            if len(seen) == 1:
                return NS(content=[NS(type="text", text="Fixed it:\n\n```python src/fib.py\ndef fib(n): ...\n```")],
                          stop_reason="end_turn", usage=NS(input_tokens=1, output_tokens=1), model=kw["model"])
            return NS(content=[NS(type="text", text="Handing the fix to engineering.")],
                      stop_reason="end_turn", usage=NS(input_tokens=1, output_tokens=1), model=kw["model"])
        return await orig(**kw)

    planner.llm.create = fake
    t = asyncio.run(b.submit("Fix the crash in fib"))
    assert len(seen) == 2
    note = seen[1]["messages"][-1]["content"]
    assert "NOT written" in note and "src/fib.py" in note and "engineering" in note
    assert "src/fib.py" not in {f["file"] for f in planner.artifacts.list_files(t.id)}
    assert any(e["kind"] == "artifact" and e["meta"].get("action") == "blocked" for e in b.task_events(t.id))
    assert "You may write only" in seen[0]["system"]


def test_ask_to_a_coordinator_says_to_delegate_fixes(tmp_path):
    b = make(tmp_path)
    msg = {"id": "m1", "kind": "ask", "text": "Why does it crash?", "to": None}
    assert "engineering" not in render(msg)
    assert "engineering" in render(msg, delegate=True)
    plan = Task(prompt="p", assignee="planner")
    code = Task(prompt="p", assignee="coder")
    lead = Task(prompt="p", assignee="eng_lead")
    assert "handover_task" in b.chat.content(plan, msg)
    assert "handover_task" in b.chat.content(lead, msg)
    assert "handover_task" not in b.chat.content(code, msg)


def test_reopen_ask_tells_the_planner_to_delegate(tmp_path):
    b = make(tmp_path)

    async def go():
        t = await b.submit("Build fib")
        await b.chat.send(t.id, "There is a TypeError when I run it", "ask")
        return t

    t = asyncio.run(go())
    assert "hand the fix to the right department" in b.tasks[t.id].context
