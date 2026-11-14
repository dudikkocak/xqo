"""Output limit per agent, and replies cut off at max_tokens being continued instead of ending the task."""
import asyncio
import json
import shutil
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from xqo.agent import CONTINUE_NOTE, MAX_CONTINUES
from xqo.config import build
from xqo.llm import DEFAULT_MAX_TOKENS, resolve_max_tokens

ROOT = Path(__file__).resolve().parent.parent


def test_resolve_max_tokens():
    cfg = {"default_max_tokens": 24000}
    assert resolve_max_tokens({"name": "a", "max_tokens": 64000}, cfg, {"default_max_tokens": 8000}) == (64000, "agent")
    assert resolve_max_tokens({"name": "a"}, cfg, {"default_max_tokens": 8000}) == (8000, "department")
    assert resolve_max_tokens({"name": "a"}, cfg) == (24000, "config")
    assert resolve_max_tokens({"name": "a"}, {}) == (DEFAULT_MAX_TOKENS, "default")
    for bad in (100, "32000", 3.5, True):
        with pytest.raises(ValueError, match="max_tokens"):
            resolve_max_tokens({"name": "a", "max_tokens": bad}, {})


def setup(tmp_path, replies, max_tokens=4000):
    cfg = json.loads((ROOT / "agents.json").read_text())
    name = next(a["name"] for a in cfg["agents"] if "planning" not in [s.lower() for s in a["skills"]]) \
        if len(cfg["agents"]) > 1 else cfg["agents"][0]["name"]
    for a in cfg["agents"]:
        if a["name"] == name:
            a["max_tokens"] = max_tokens
            a.pop("write_paths", None)  # the scripted replies write src/ files
    p = tmp_path / "agents.json"
    p.write_text(json.dumps(cfg))
    if (ROOT / "departments").is_dir():
        shutil.copytree(ROOT / "departments", tmp_path / "departments")
    b = build(p, tmp_path / "a", str(tmp_path / "e.jsonl"), mock=True)
    agent, calls = b.agents[name], []

    async def create(*, messages, max_tokens, **_):
        calls.append({"messages": list(messages), "max_tokens": max_tokens})
        text, stop = replies[min(len(calls), len(replies)) - 1]
        return NS(content=[NS(type="text", text=text)], stop_reason=stop,
                  usage=NS(input_tokens=1, output_tokens=1), model="m")

    agent.llm = NS(create=create)
    return b, agent, calls


def test_cut_reply_is_continued_and_files_extracted_whole(tmp_path):
    b, agent, calls = setup(tmp_path, [("Here:\n\n```python src/a.py\nx = 1\n", "max_tokens"),
                                       ("y = 2\n```\n\nDone.", "end_turn")])
    assert agent.max_tokens == 4000 and agent.max_tokens_source == "agent"
    t = asyncio.run(b.submit("write a", agent=agent.name))
    assert t.status == "done" and "truncated" not in t.result
    assert (b.artifacts.workspace(t.id) / "src" / "a.py").read_text() == "x = 1\ny = 2\n"
    assert calls[0]["max_tokens"] == 4000 and len(calls) == 2
    last = calls[1]["messages"][-1]
    assert last["role"] == "user" and last["content"] == CONTINUE_NOTE.format(n=4000)
    assert agent.cutoffs == {"cut": 1, "continued": 1, "gave_up": 0}
    assert agent.describe()["cutoffs"]["cut"] == 1


def test_gives_up_after_continuing_and_asks_the_user(tmp_path):
    b, agent, calls = setup(tmp_path, [("```python src/b.py\nz = 1\n", "max_tokens")])
    b.attention.timeout = 0.3  # the question auto-confirms "stop"
    t = asyncio.run(b.submit("write b", agent=agent.name))
    assert len(calls) == MAX_CONTINUES + 1
    assert "[truncated: max_tokens (4000) reached after continuing]" in t.result
    assert not (b.artifacts.workspace(t.id) / "src" / "b.py").exists()  # an unfinished block is only a snippet
    assert agent.cutoffs == {"cut": 3, "continued": 2, "gave_up": 1}
    att = next(iter(b.attention.items.values()))
    assert att.kind == "output_limit"
