"""xqo-feeds is found through its entry point, keeps its state folder in backups, and needs xqo-schedules."""
import json

from fastapi.testclient import TestClient

from xqo import backup
from xqo import plugin as plugin_mod
from xqo.api import build
from xqo.web import create_app

CFG = {"agents": [{"name": "planner", "skills": ["planning"]}],
       "feeds": {"backup": {"type": "push", "token_env": "TEST_FEED_TOKEN"}}}


def make(tmp_path):
    cfg = tmp_path / "agents.json"
    cfg.write_text(json.dumps(CFG))
    return build(cfg, tmp_path / "artifacts", str(tmp_path / "logs" / "events.jsonl"), mock=True, departments="none")


def test_plugin_is_found_and_serves_its_ui(tmp_path):
    bus = make(tmp_path)
    st = bus.plugins.states["feeds"]
    assert st.state == "active" and st.source == "xqo_feeds.plugin:FeedsPlugin"
    assert "feeds" in backup.state_dirs()
    with TestClient(create_app(bus)) as c:
        assert c.get("/plugins/feeds/ui.js").status_code == 200


def test_without_schedules_feeds_is_a_missing_warning(tmp_path, monkeypatch):
    real = plugin_mod.available
    monkeypatch.setattr(plugin_mod, "available", lambda: {k: v for k, v in real().items() if k != "schedules"})
    bus = make(tmp_path)  # no error: startup goes on
    st = bus.plugins.states
    assert st["schedules"].state == "missing" and st["feeds"].state == "missing"
    assert any("feeds" in w for w in bus.config_warnings)
