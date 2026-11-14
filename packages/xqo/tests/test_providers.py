"""Optional provider per agent: Anthropic by default, or any OpenAI-compatible API (OpenAI, Ollama, a URL)."""
import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from xqo.config import build
from xqo.providers import (OpenAICompatLLM, ProviderError, default_provider, from_openai,
                                    resolve_provider, to_openai_messages, to_openai_tools)
from xqo.web import create_app

ROOT = Path(__file__).resolve().parent.parent


def test_resolve_provider_order(monkeypatch):
    monkeypatch.delenv("XQO_PROVIDER", raising=False)
    monkeypatch.setattr("xqo.providers.sdk_installed", lambda: False)  # see test_sdk_backend.py
    cfg = {"default_provider": "ollama",
           "providers": {"local": {"base_url": "http://localhost:8001/v1", "api_key_env": "LOCAL_KEY"}}}
    name, spec, src = resolve_provider({"name": "a", "provider": "openai"}, cfg, {"default_provider": "local"})
    assert (name, spec["type"], spec["api_key_env"], src) == ("openai", "openai", "OPENAI_API_KEY", "agent")
    name, spec, src = resolve_provider({"name": "a"}, cfg, {"default_provider": "local"})
    assert (name, spec["base_url"], src) == ("local", "http://localhost:8001/v1", "department")
    assert resolve_provider({"name": "a"}, cfg)[::2] == ("ollama", "config")
    name, spec, src = resolve_provider({"name": "a"}, {})
    assert (name, spec["type"], src) == ("anthropic", "anthropic", "default")
    name, spec, _ = resolve_provider({"name": "a", "provider": "http://gpu:8000/v1/", "api_key_env": "GPU_KEY"}, {})
    assert spec == {"type": "openai", "base_url": "http://gpu:8000/v1", "api_key_env": "GPU_KEY"}
    with pytest.raises(ValueError, match="unknown provider"):
        resolve_provider({"name": "a", "provider": "nope"}, {})
    with pytest.raises(ValueError, match="api_key_env"):  # keys never live in the config
        resolve_provider({"name": "a"}, {"providers": {"x": {"base_url": "http://x/v1", "api_key": "sk-123"}}})
    with pytest.raises(ValueError, match="base_url"):
        resolve_provider({"name": "a"}, {"providers": {"x": {"type": "openai"}}})


def test_message_and_response_mapping():
    msgs = [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAA"}},
                {"type": "document", "title": "notes.txt", "source": {"type": "text", "data": "hello"}},
                {"type": "text", "text": "Build it"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "Writing."},
                                              {"type": "tool_use", "id": "t1", "name": "write_file",
                                               "input": {"path": "a.py", "content": "x=1"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "saved a.py"},
                                         {"type": "text", "text": "Message from the user (steer): faster"}]}]
    out = to_openai_messages("sys", msgs)
    assert out[0] == {"role": "system", "content": "sys"}
    assert out[1]["content"][0] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}
    assert "--- notes.txt ---\nhello" in out[1]["content"][1]["text"]
    assert out[2]["tool_calls"][0]["function"] == {"name": "write_file",
                                                   "arguments": json.dumps({"path": "a.py", "content": "x=1"})}
    assert out[3] == {"role": "tool", "tool_call_id": "t1", "content": "saved a.py"}
    assert out[4] == {"role": "user", "content": "Message from the user (steer): faster"}
    tools = to_openai_tools([{"name": "f", "description": "d", "input_schema": {"type": "object"}},
                             {"type": "mcp_toolset", "mcp_server_name": "remote"}])
    assert tools == [{"type": "function", "function": {"name": "f", "description": "d",
                                                       "parameters": {"type": "object"}}}]
    r = from_openai({"model": "gpt-x", "choices": [{"finish_reason": "stop", "message": {
        "content": "ok", "tool_calls": [{"id": "c1", "function": {"name": "f", "arguments": "{\"a\": 1}"}}]}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 7, "prompt_tokens_details": {"cached_tokens": 40}}}, "m")
    assert r.stop_reason == "tool_use" and r.content[1].input == {"a": 1} and r.model == "gpt-x"
    assert (r.usage.input_tokens, r.usage.output_tokens, r.usage.cache_read_input_tokens) == (60, 7, 40)


def test_missing_key_and_errors(monkeypatch):
    monkeypatch.delenv("NO_SUCH_KEY", raising=False)
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(400, text="bad model")

    llm = OpenAICompatLLM({"type": "openai", "base_url": "http://x/v1", "api_key_env": "NO_SUCH_KEY"},
                          transport=httpx.MockTransport(handler), retries=0)
    with pytest.raises(ProviderError, match="NO_SUCH_KEY is not set"):
        asyncio.run(llm.create(model="m", system="s", messages=[{"role": "user", "content": "hi"}], tools=[]))
    assert not calls  # nothing is sent without the key
    monkeypatch.setenv("NO_SUCH_KEY", "sk-test")
    with pytest.raises(ProviderError, match="400"):
        asyncio.run(llm.create(model="m", system="s", messages=[{"role": "user", "content": "hi"}], tools=[]))
    assert calls[0].headers["authorization"] == "Bearer sk-test"


def test_anthropic_without_credentials_says_how_to_fix(monkeypatch, tmp_path):
    """No key, token or profile: a clear message instead of the SDK's bare TypeError."""
    from xqo.llm import AnthropicLLM
    for v in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE", "ANTHROPIC_CONFIG_DIR"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    llm = AnthropicLLM(spec={"type": "anthropic", "api_key_env": "ANTHROPIC_API_KEY"})
    msgs = [{"role": "user", "content": "hi"}]
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY is not set"):
        asyncio.run(llm.create(model="m", system="s", messages=msgs, tools=[]))
    with pytest.raises(RuntimeError, match="PowerShell"):
        asyncio.run(llm.title("hi"))


def test_agent_loop_on_openai_compatible_provider(tmp_path, monkeypatch):
    """A whole task on a fake OpenAI-compatible server: tool calls, files, artifacts, usage, title."""
    monkeypatch.setenv("FAKE_LLM_KEY", "sk-fake")
    cfg = json.loads((ROOT / "agents.json").read_text())
    cfg["providers"] = {"fake": {"base_url": "http://fake.local/v1", "api_key_env": "FAKE_LLM_KEY"}}
    cfg["default_provider"] = "fake"
    cfg["default_model"] = "fake-model"
    cfg["agents"] = [{"name": "solo", "skills": ["python"], "system": "You write code."}]
    cfg.pop("departments", None)
    p = tmp_path / "agents.json"
    p.write_text(json.dumps(cfg))
    bodies = []

    def handler(req):
        body = json.loads(req.content)
        bodies.append(body)
        assert req.headers["authorization"] == "Bearer sk-fake" and req.url.path == "/v1/chat/completions"
        if "one-line title" in body["messages"][0]["content"]:
            msg = {"content": "Write a helper module"}
        elif not any(m["role"] == "tool" for m in body["messages"]):
            msg = {"content": "Writing the file.", "tool_calls": [{"id": "call_1", "type": "function", "function": {
                "name": "write_file", "arguments": json.dumps({"path": "src/util.py", "content": "X = 1\n"})}}]}
        else:
            msg = {"content": "Done:\n\n```python src/helper.py\ndef helper():\n    return 42\n```\n"}
        return httpx.Response(200, json={"model": "fake-model-001", "choices": [{"message": msg, "finish_reason":
                              "tool_calls" if "tool_calls" in msg else "stop"}],
                              "usage": {"prompt_tokens": 50, "completion_tokens": 10}})

    b = build(p, tmp_path / "art", str(tmp_path / "ev.jsonl"), mock=False)
    coder = b.agents["solo"]
    assert (coder.provider, coder.provider_type, coder.provider_source) == ("fake", "openai", "config")
    coder.llm.client = httpx.AsyncClient(base_url="http://fake.local/v1/", transport=httpx.MockTransport(handler))
    t = asyncio.run(b.submit("Write a helper module", agent="solo"))
    assert t.status == "done", t.result
    ws = b.artifacts.workspace(t.id)
    assert (ws / "src" / "util.py").read_text() == "X = 1\n"
    assert "return 42" in (ws / "src" / "helper.py").read_text()
    work = [x for x in bodies if "one-line title" not in x["messages"][0]["content"]]
    assert {f["function"]["name"] for f in work[0]["tools"]} >= {"write_file", "read_file", "reply_user"}
    assert work[1]["messages"][-1]["role"] == "tool" and work[1]["messages"][-1]["tool_call_id"] == "call_1"
    assert coder.usage["calls"] == 2 and coder.usage["input_tokens"] == 100
    assert coder.served_model == "fake-model-001" and len(work) < len(bodies)  # the title came from it too
    with TestClient(create_app(b)) as c:
        a = next(x for x in c.get("/api/agents").json() if x["name"] == "solo")
        assert a["provider"] == "fake" and a["provider_info"] == {
            "type": "openai", "base_url": "http://fake.local/v1", "api_key_env": "FAKE_LLM_KEY", "key_set": True}
        prov = c.get("/api/config").json()["providers"]
        assert "solo" in prov["fake"]["agents"] and "sk-fake" not in json.dumps(prov)


def test_mock_mode_keeps_provider_labels(tmp_path):
    cfg = json.loads((ROOT / "agents.json").read_text())
    cfg["agents"][-1]["provider"] = "ollama"
    p = tmp_path / "agents.json"
    p.write_text(json.dumps(cfg))
    if (ROOT / "departments").is_dir():
        import shutil
        shutil.copytree(ROOT / "departments", tmp_path / "departments")
    b = build(p, tmp_path / "a", str(tmp_path / "e.jsonl"), mock=True)
    a = b.agents[cfg["agents"][-1]["name"]].describe()
    assert (a["provider"], a["provider_source"], a["provider_info"]["key_set"]) == ("ollama", "agent", None)
    others = [x.describe()["provider"] for n, x in b.agents.items() if n != cfg["agents"][-1]["name"]]
    assert set(others) == {default_provider()}  # claude-sdk when installed


def test_department_default_provider(tmp_path):
    import shutil
    shutil.copy(ROOT / "agents.json", tmp_path / "agents.json")
    shutil.copytree(ROOT / "departments", tmp_path / "departments")
    p = tmp_path / "departments" / "marketing.json"
    d = json.loads(p.read_text())
    d["default_provider"] = "ollama"
    d["agents"][0]["provider"] = "openai"
    p.write_text(json.dumps(d))
    b = build(tmp_path / "agents.json", tmp_path / "a", str(tmp_path / "e.jsonl"), mock=True)
    first, rest = d["agents"][0]["name"], [a["name"] for a in d["agents"][1:]]
    assert (b.agents[first].provider, b.agents[first].provider_source) == ("openai", "agent")
    assert rest and all((b.agents[n].provider, b.agents[n].provider_source) == ("ollama", "department")
                        for n in rest)
    assert (b.agents["eng_lead"].provider, b.agents["eng_lead"].provider_source) == (default_provider(), "default")
    assert set(b.providers["ollama"]["agents"]) == set(rest)
