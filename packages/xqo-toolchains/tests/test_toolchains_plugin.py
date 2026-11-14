"""xqo-toolchains is found through its entry point and installs itself into the sandbox."""
import json

from xqo.api import build, current_toolchains

from xqo_toolchains import chains


def test_plugin_is_found_and_active(tmp_path):
    cfg = tmp_path / "agents.json"
    cfg.write_text(json.dumps({"agents": [{"name": "planner", "skills": ["planning"]}]}))
    bus = build(cfg, tmp_path / "artifacts", str(tmp_path / "logs" / "events.jsonl"), mock=True, departments="none")
    st = bus.plugins.states["toolchains"]
    assert st.state == "active" and st.source == "xqo_toolchains:ToolchainsPlugin"
    kinds = bus.plugins.service("toolchains")["kinds"]
    assert "node" in kinds and "go" in kinds
    assert current_toolchains() is chains
