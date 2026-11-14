"""Sandbox, shared notes board, MCP, rollback and packaging bits."""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from xqo.agent import Agent
from xqo.artifacts import ArtifactStore
from xqo.bus import MessageBus
from xqo.config import build, load_config
from xqo.mcp_hub import MCPHub, remote_params
from xqo.memory import MemoryBoard
from xqo.sandbox import Sandbox, SandboxConfig
from xqo.web import create_app

ROOT = Path(__file__).resolve().parent.parent


def text(t):
    return NS(content=[NS(type="text", text=t)], stop_reason="end_turn", usage=None, model="m")


# ------------------------------------------------------------------ sandbox
def test_local_sandbox_runs_on_workspace_copy(tmp_path):
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "m.py").write_text("print('hi from m')\n")
    sb = Sandbox(SandboxConfig(backend="local", timeout=10))
    r = asyncio.run(sb.run(ws, f"{sys.executable} src/m.py && echo changed > src/m.py"))
    assert r["exit_code"] == 0 and "hi from m" in r["stdout"]
    assert (ws / "src" / "m.py").read_text() == "print('hi from m')\n"  # workspace untouched


def test_sandbox_timeout_and_failure(tmp_path):
    sb = Sandbox(SandboxConfig(backend="local", timeout=0.5))
    r = asyncio.run(sb.run(tmp_path, f"{sys.executable} -c 'import time; time.sleep(5)'"))
    assert r["timed_out"]
    r = asyncio.run(sb.run(tmp_path, f"{sys.executable} -c 'raise SystemExit(3)'"))
    assert r["exit_code"] == 3


def test_sandbox_off_and_auto(tmp_path, monkeypatch):
    with pytest.raises(RuntimeError):
        asyncio.run(Sandbox(SandboxConfig(backend="off")).run(tmp_path, "true"))
    monkeypatch.setattr("xqo.sandbox.docker_available", lambda: False)
    assert Sandbox(SandboxConfig(backend="auto")).backend == "local"


def test_tester_flow_runs_code(tmp_path, monkeypatch):
    monkeypatch.setenv("XQO_SANDBOX", "local")
    b = build(ROOT / "agents.json", tmp_path / "art", str(tmp_path / "ev.jsonl"), mock=True)
    t = asyncio.run(b.submit("Build fib and run tests"))
    ends = [e for e in b.history if e["kind"] == "sandbox" and e["meta"].get("phase") == "end"]
    assert ends and ends[0]["meta"]["exit_code"] == 0
    assert any(r["action"] == "run" for r in b.artifacts.list(t.id))


def test_run_code_only_for_allowed_agents(tmp_path):
    store = ArtifactStore(tmp_path / "a")
    a = Agent("x", ["y"], "s", None, store, can_run_code=False)
    b = Agent("t", ["testing"], "s", None, store, can_run_code=True)
    bus = MessageBus(tmp_path / "e.jsonl")
    bus.artifacts, bus.sandbox = store, Sandbox(SandboxConfig(backend="local"))
    bus.register(a)
    bus.register(b)
    assert "run_code" not in [t["name"] for t in a.tools()[0]]
    assert "run_code" in [t["name"] for t in b.tools()[0]]


# ------------------------------------------------------------------ memory
def test_memory_board(tmp_path):
    m = MemoryBoard(tmp_path)
    m.set("R", "db", "postgres", "planner")
    m.set("R", "db", "sqlite", "coder")
    m.set("R", "port", "8080", "coder")
    assert m.notes("R")["db"]["value"] == "sqlite" and len(m.log("R")) == 3
    m.set("R", "port", "", "coder")
    assert "port" not in m.notes("R")
    assert "db: sqlite" in m.render("R")
    with pytest.raises(ValueError):
        m.set("R", "missing", "", "x")


def test_notes_shared_with_sub_agents(tmp_path):
    b = build(ROOT / "agents.json", tmp_path / "art", str(tmp_path / "ev.jsonl"), mock=True)
    seen = []
    coder = b.agents["coder"]
    real = coder.llm.create

    async def spy(**kw):
        if kw["agent"] == "coder":
            seen.append(kw["messages"][0]["content"])
        return await real(**kw)

    coder.llm = NS(create=spy)
    t = asyncio.run(b.submit("Build fib"))
    assert "approach: iterative fib" in seen[0]  # planner's note reached the coder
    assert b.memory.notes(t.id)["approach"]["author"] == "planner"
    assert any(e["kind"] == "memory" for e in b.history)


# ------------------------------------------------------------------ MCP
def test_remote_mcp_params(monkeypatch):
    monkeypatch.setenv("TOK", "secret")
    servers, toolsets = remote_params([
        {"name": "docs", "url": "https://x/mcp", "token_env": "TOK", "allowed_tools": ["search"]},
        {"name": "local", "command": "x"}])
    assert servers == [{"type": "url", "url": "https://x/mcp", "name": "docs", "authorization_token": "secret"}]
    assert toolsets == [{"type": "mcp_toolset", "mcp_server_name": "docs", "default_config": {"enabled": False},
                         "configs": {"search": {"enabled": True}}}]


def test_anthropic_llm_adds_mcp_beta(monkeypatch):
    from xqo import llm as llmmod

    captured = {}

    class FakeStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get_final_message(self):
            return text("ok")

    class FakeMessages:  # calls are streamed
        def stream(self, **kw):
            captured.update(kw)
            return FakeStream()

    obj = llmmod.AnthropicLLM.__new__(llmmod.AnthropicLLM)
    obj.client, obj.fallbacks = NS(beta=NS(messages=FakeMessages())), True
    asyncio.run(obj.create(model="claude-opus-5-5", system="s", messages=[], tools=[],
                           mcp_servers=[{"type": "url", "url": "u", "name": "n"}]))
    assert set(captured["betas"]) == {"server-side-fallback-2026-07-01", "mcp-client-2025-11-20"}
    assert captured["mcp_servers"][0]["name"] == "n" and captured["fallbacks"] == "default"
    assert captured["max_tokens"] == llmmod.DEFAULT_MAX_TOKENS


def test_local_stdio_mcp_tools(tmp_path):
    pytest.importorskip("mcp")
    server = {"name": "echo", "command": sys.executable, "args": [str(ROOT / "tests" / "mcp_echo_server.py")]}

    async def scenario():
        hub = MCPHub([server])
        await hub.start()
        try:
            assert not hub.errors, hub.errors
            names = [d["name"] for d in hub.tool_defs(["echo"])]
            assert set(names) == {"mcp__echo__echo", "mcp__echo__add"}
            assert await hub.call("mcp__echo__echo", {"text": "hi"}) == ("HI", False)

            calls = []

            class LLM:
                async def create(self, *, messages, tools, **_):
                    calls.append([t["name"] for t in tools])
                    if len(calls) == 1:
                        return NS(content=[NS(type="tool_use", id="t1", name="mcp__echo__add", input={"a": 2, "b": 3})],
                                  stop_reason="tool_use", usage=None, model="m")
                    return text(messages[-1]["content"][0]["content"])

            store = ArtifactStore(tmp_path / "a")
            bus = MessageBus(tmp_path / "e.jsonl")
            bus.artifacts, bus.mcp = store, hub
            bus.register(Agent("a", ["x"], "s", LLM(), store, mcp_servers=[server]))
            t = await bus.submit("add")
            assert t.result == "5" and "mcp__echo__add" in calls[0]
            assert any(e["kind"] == "mcp" for e in bus.history)
        finally:
            await hub.stop()

    asyncio.run(scenario())


def test_bad_local_mcp_server_reports_error():
    async def scenario():
        hub = MCPHub([{"name": "nope", "command": "definitely-not-a-real-binary-xyz"}])
        await hub.start()
        st = hub.status()
        await hub.stop()
        return st

    st = asyncio.run(scenario())
    assert st[0]["connected"] is False and st[0]["error"]


# ------------------------------------------------------------- web + rollback
def test_web_memory_rollback_config(tmp_path):
    b = build(ROOT / "agents.json", tmp_path / "art", str(tmp_path / "ev.jsonl"), mock=True)
    t = asyncio.run(b.submit("Build fib"))
    with TestClient(create_app(b)) as c:
        cfg = c.get("/api/config").json()
        assert cfg["attention_timeout"] > 0 and "backend" in cfg["sandbox"]
        assert c.post(f"/api/memory/{t.id}", json={"key": "owner", "value": "dev"}).status_code == 200
        notes = c.get(f"/api/memory/{t.id}").json()["notes"]
        assert {n["key"] for n in notes} == {"approach", "owner"}
        hist = c.get(f"/api/workspace/{t.id}/history/src/fib.py").json()
        assert [h["action"] for h in hist] == ["create", "patch"]
        r = c.post(f"/api/workspace/{t.id}/rollback", json={"file": "src/fib.py", "version": hist[0]["path"]})
        assert r.status_code == 200 and r.json()["action"] == "rollback"
        assert "raise ValueError" not in c.get(f"/api/workspace/{t.id}/file/src/fib.py").text
        assert len(c.get(f"/api/workspace/{t.id}/history/src/fib.py").json()) == 3
        assert c.post(f"/api/workspace/{t.id}/rollback", json={"file": "src/fib.py", "version": "../x"}).status_code == 404


def test_packaged_default_config(tmp_path):
    cfg = load_config(tmp_path / "missing.json")
    assert json.loads((ROOT / "agents.json").read_text()) == cfg
    from xqo.departments import PACKAGED_DEPARTMENTS, load_files
    depts = load_files(PACKAGED_DEPARTMENTS)
    assert any(a.get("can_run_code") for d in depts for a in d["agents"])
    for d in depts:  # the packaged examples match the repo's departments/ folder
        assert json.loads((ROOT / "departments" / f"{d['name']}.json").read_text()) == {
            k: v for k, v in d.items() if k != "_source"}
