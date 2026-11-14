"""xqo-schedules is found through its entry point, serves its dashboard script and tells backups what to skip."""
import json

from fastapi.testclient import TestClient

from xqo import backup
from xqo.api import build
from xqo.web import create_app


def test_plugin_is_found_serves_its_ui_and_skips_its_lock(tmp_path):
    cfg = tmp_path / "agents.json"
    cfg.write_text(json.dumps({"agents": [{"name": "planner", "skills": ["planning"]}]}))
    bus = build(cfg, tmp_path / "artifacts", str(tmp_path / "logs" / "events.jsonl"), mock=True, departments="none")
    st = bus.plugins.states["schedules"]
    assert st.state == "active" and st.source == "xqo_schedules:SchedulesPlugin"
    assert bus.plugins.service("schedules") is not None
    assert "scheduler.lock" in backup.skip_files()
    with TestClient(create_app(bus)) as c:
        row = {p["name"]: p for p in c.get("/api/plugins").json()["items"]}["schedules"]
        assert row["ui"] == ["/plugins/schedules/ui.js"]
        assert c.get("/plugins/schedules/ui.js").status_code == 200
        assert c.get("/api/schedules").status_code == 200
