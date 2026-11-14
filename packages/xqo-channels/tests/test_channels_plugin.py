"""xqo-channels is found through its entry point and keeps its state folder in backups."""
import json

from xqo import backup
from xqo.api import build


def test_plugin_is_found_and_inactive_without_config(tmp_path):
    cfg = tmp_path / "agents.json"
    cfg.write_text(json.dumps({"agents": [{"name": "planner", "skills": ["planning"]}]}))
    bus = build(cfg, tmp_path / "artifacts", str(tmp_path / "logs" / "events.jsonl"), mock=True, departments="none")
    st = bus.plugins.states["channels"]
    assert st.state == "inactive" and st.source == "xqo_channels.plugin:ChannelsPlugin"
    assert "channels" in backup.state_dirs()
