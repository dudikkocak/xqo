"""System-wide "no limits" hour: max_turns / max_tokens, task time and budgets off for every agent, running tasks included."""
import asyncio
import json
import shutil
from pathlib import Path
from types import SimpleNamespace as NS

from fastapi.testclient import TestClient

from xqo.config import build
from xqo.limits import OVERRIDE_SECONDS, UNLIMITED_MAX_TOKENS, Limits
from xqo.web import create_app

ROOT = Path(__file__).resolve().parent.parent


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_override_lasts_an_hour_restarts_and_persists(tmp_path):
    clock, path = Clock(), tmp_path / "limits_override.json"
    lim = Limits(path, clock)
    assert not lim.active() and lim.max_turns(12) == 12 and lim.max_tokens(4000) == 4000
    st = lim.enable()
    assert st["active"] and st["remaining"] == OVERRIDE_SECONDS
    assert lim.max_turns(12) == float("inf") and lim.max_tokens(4000) == UNLIMITED_MAX_TOKENS
    clock.now += 3000
    assert lim.status()["remaining"] == OVERRIDE_SECONDS - 3000
    assert lim.enable()["remaining"] == OVERRIDE_SECONDS  # turned on again: a fresh hour
    assert Limits(path, clock).active()  # survives a restart
    clock.now += OVERRIDE_SECONDS
    assert not lim.active() and lim.max_turns(12) == 12 and lim.status() == {**lim.status(), "active": False, "until": None}
    lim.enable()
    assert not lim.disable()["active"] and not Limits(path, clock).active()


def setup(tmp_path, replies, max_turns=2, max_tokens=4000):
    cfg = json.loads((ROOT / "agents.json").read_text())
    name = cfg["agents"][0]["name"]
    for a in cfg["agents"]:
        a["max_turns"], a["max_tokens"] = max_turns, max_tokens
    p = tmp_path / "agents.json"
    p.write_text(json.dumps(cfg))
    if (ROOT / "departments").is_dir():
        shutil.copytree(ROOT / "departments", tmp_path / "departments")
    b = build(p, tmp_path / "a", str(tmp_path / "e.jsonl"), mock=True)
    b.limits.clock = clock = Clock()
    agent, calls = b.agents[name], []

    async def create(*, max_tokens, **_):
        calls.append(max_tokens)
        step = replies[len(calls) - 1] if len(calls) <= len(replies) else None
        if callable(step):
            step(b)
        if step == "done" or len(calls) >= len(replies):
            return NS(content=[NS(type="text", text="All done.")], stop_reason="end_turn",
                      usage=NS(input_tokens=1, output_tokens=1), model="m")
        return NS(content=[NS(type="tool_use", id=f"t{len(calls)}", name="read_notes", input={})],
                  stop_reason="tool_use", usage=NS(input_tokens=1, output_tokens=1), model="m")

    agent.llm = NS(create=create)
    return b, agent, calls, clock


def test_limits_apply_without_the_override(tmp_path):
    b, agent, calls, _ = setup(tmp_path, ["tool"] * 5 + ["done"])
    t = asyncio.run(b.submit("work", agent=agent.name))
    assert calls == [4000, 4000] and t.result.endswith("[stopped: max turns reached]")


def test_turned_on_while_a_task_runs(tmp_path):
    """The agent's max_turns is 2; the override, turned on during turn 1, lets it run to its end."""
    b, agent, calls, _ = setup(tmp_path, [lambda b: b.limits.enable(), "tool", "tool", "tool", "done"])
    t = asyncio.run(b.submit("work", agent=agent.name))
    assert t.status == "done" and "max turns" not in t.result and len(calls) == 5
    assert calls == [4000] + [UNLIMITED_MAX_TOKENS] * 4  # the first call was already under way


def test_reverts_when_the_hour_runs_out(tmp_path):
    def expire(b):
        b.limits.clock.now += OVERRIDE_SECONDS + 1
    b, agent, calls, clock = setup(tmp_path, ["tool", "tool", expire, "tool", "tool", "done"])
    b.limits.enable()
    t = asyncio.run(b.submit("work", agent=agent.name))
    assert calls == [UNLIMITED_MAX_TOKENS] * 2 + [UNLIMITED_MAX_TOKENS]  # turn 3 started inside the hour
    assert t.result.endswith("[stopped: max turns reached]")


def test_api_is_system_wide_and_restarts_the_hour(tmp_path):
    bus = build(ROOT / "agents.json", tmp_path / "a", str(tmp_path / "e.jsonl"), mock=True)
    bus.limits.clock = clock = Clock()
    with TestClient(create_app(bus)) as c:
        assert c.get("/api/limits").json()["active"] is False
        on = c.post("/api/limits", json={"enabled": True}).json()
        assert on["active"] and on["remaining"] == OVERRIDE_SECONDS
        clock.now += 1800
        assert c.get("/api/limits").json()["remaining"] == 1800  # what another browser tab sees
        assert c.post("/api/limits", json={}).json()["remaining"] == OVERRIDE_SECONDS  # retriggered
        assert c.post("/api/limits", json={"enabled": False}).json()["active"] is False
    assert [e["content"] for e in bus.history if e["kind"] == "limits"] == [
        "resource limits (turns, output size, task time, budgets) off for one hour"] * 2 + ["resource limits back on"]
    assert (tmp_path / "a" / "limits_override.json").is_file()


def test_one_object_holds_sizes_and_the_override(tmp_path, monkeypatch):
    monkeypatch.delenv("XQO_IMAGE_MB", raising=False)
    lim = Limits.from_config({"image_mb": 6}, tmp_path / "o.json")
    try:
        d = lim.describe()
        assert d["sizes"]["image_mb"] == 6 and d["override"]["active"] is False
        lim.enable("me")
        assert Limits(tmp_path / "o.json").active()
    finally:
        Limits.from_config(None)  # other tests expect the built-in sizes
