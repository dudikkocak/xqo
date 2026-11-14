"""The schedules plugin's dashboard button, loaded from the package's ui.js (skipped without playwright / chromium)."""
import json

import pytest

from xqo.api import build
from xqo.testing import LiveServer, launch_chromium
from xqo.web import create_app


def test_dashboard_button_opens_the_schedules_panel(tmp_path):
    pw = pytest.importorskip("playwright.sync_api")
    cfg = tmp_path / "agents.json"
    cfg.write_text(json.dumps({"agents": [{"name": "planner", "skills": ["planning"]}]}))
    bus = build(cfg, tmp_path / "artifacts", str(tmp_path / "events.jsonl"), mock=True, departments="none")
    errors = []
    with LiveServer(create_app(bus)) as srv, pw.sync_playwright() as p:
        b = launch_chromium(p)
        pg = b.new_page()
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(srv.url + "/")
        pg.wait_for_selector("#tsched", state="visible", timeout=20000)
        pg.click("#tsched")
        pg.wait_for_function("document.getElementById('dlg').dataset.kind === 'schedules'", timeout=10000)
        b.close()
    assert not errors, errors
