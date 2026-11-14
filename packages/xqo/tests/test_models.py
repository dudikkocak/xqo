"""Optional model (and effort) per agent, with fallbacks to the configured and built-in defaults."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from xqo.config import build
from xqo.llm import DEFAULT_MODEL, resolve_model
from xqo.web import create_app

ROOT = Path(__file__).resolve().parent.parent


def test_resolve_model_order():
    cfg = {"default_model": "claude-sonnet-5-5", "default_effort": "low"}
    assert resolve_model({"name": "a", "model": "claude-haiku-5-5", "effort": "high"}, cfg) == \
        ("claude-haiku-5-5", "high", "agent")
    assert resolve_model({"name": "a"}, cfg, {"default_model": "claude-fable-5-1"}) == \
        ("claude-fable-5-1", "low", "department")
    assert resolve_model({"name": "a"}, cfg) == ("claude-sonnet-5-5", "low", "config")
    assert resolve_model({"name": "a"}, {}) == (DEFAULT_MODEL, "medium", "default")
    with pytest.raises(ValueError, match="non-empty"):
        resolve_model({"name": "a", "model": ""}, cfg)
    with pytest.raises(ValueError, match="effort"):
        resolve_model({"name": "a", "effort": "turbo"}, cfg)


def test_agents_get_their_own_model(tmp_path):
    cfg = json.loads((ROOT / "agents.json").read_text())
    cfg["agents"][-1]["model"] = "claude-haiku-5-5"
    cfg["agents"][-1]["effort"] = "low"
    p = tmp_path / "agents.json"
    p.write_text(json.dumps(cfg))
    if (ROOT / "departments").is_dir():  # keep the department files next to the config
        import shutil
        shutil.copytree(ROOT / "departments", tmp_path / "departments")
    b = build(p, tmp_path / "a", str(tmp_path / "e.jsonl"), mock=True)
    coder = b.agents[cfg["agents"][-1]["name"]]
    other = next(a for a in b.agents.values() if a is not coder)
    assert (coder.model, coder.effort, coder.model_source) == ("claude-haiku-5-5", "low", "agent")
    assert other.model == cfg["default_model"] and other.model_source == "config"
    with TestClient(create_app(b)) as c:
        row = next(a for a in c.get("/api/agents").json() if a["name"] == coder.name)
        assert row["model"] == "claude-haiku-5-5" and row["model_source"] == "agent" and row["effort"] == "low"


def test_department_default_model(tmp_path):
    import shutil
    shutil.copy(ROOT / "agents.json", tmp_path / "agents.json")
    shutil.copytree(ROOT / "departments", tmp_path / "departments")
    p = tmp_path / "departments" / "marketing.json"
    d = json.loads(p.read_text())
    d["default_model"] = "claude-sonnet-5-5"
    d["agents"][0]["model"] = "claude-haiku-5-5"
    p.write_text(json.dumps(d))
    b = build(tmp_path / "agents.json", tmp_path / "a", str(tmp_path / "e.jsonl"), mock=True)
    first, rest = d["agents"][0]["name"], [a["name"] for a in d["agents"][1:]]
    assert (b.agents[first].model, b.agents[first].model_source) == ("claude-haiku-5-5", "agent")
    assert rest and all((b.agents[n].model, b.agents[n].model_source) == ("claude-sonnet-5-5", "department")
                        for n in rest)
    assert b.agents["eng_lead"].model_source == "config"  # other departments keep the global default
