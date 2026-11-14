"""Office view: editing only with `serve --edit`, the HQ name, the overall usage total, the instant "got it",
finished tasks, speech bubbles, following an agent and the windows' time of day."""
import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from xqo.plugins.office.view import hq_name
from xqo.usage import UsageTotals
from xqo.web import create_app
from test_attachments import mock_bus
from test_office import _bus, _config, _open, browser  # noqa: F401 (browser: the fixture)
from xqo.config import build

ROOT = Path(__file__).resolve().parent.parent


def test_office_editing_needs_the_edit_flag(tmp_path):
    bus = _bus(tmp_path, edit=False)
    client = TestClient(create_app(bus))
    assert client.get("/api/office").json()["editable"] is False
    assert client.get("/api/office/layout").status_code == 200  # reading stays open
    for r in (client.put("/api/office/layout", json={}), client.put("/api/office/layout?check=true", json={}),
              client.post("/api/office/assets", json={"name": "a.glb", "data": "Z2xURg=="}),
              client.delete("/api/office/assets/a.glb")):
        assert r.status_code == 403 and "--edit" in r.json()["detail"]
    assert not (Path(bus.artifacts.root) / "office.json").exists()
    bus.office_edit = True
    assert client.get("/api/office").json()["editable"] is True
    assert client.put("/api/office/layout", json={}).status_code == 200


def test_serve_has_an_edit_flag():
    from xqo import __main__ as cli
    src = Path(cli.__file__).read_text()
    assert '"--edit"' in src and "bus.office_edit = a.edit" in src


def test_hq_name_in_the_headers(tmp_path):
    cfg = json.loads((ROOT / "agents.json").read_text())
    cfg["name"] = "Acme <HQ>"
    p = tmp_path / "named.json"
    p.write_text(json.dumps(cfg))
    bus = build(p, tmp_path / "art", str(tmp_path / "e.jsonl"), mock=True, departments_dir=ROOT / "departments")
    client = TestClient(create_app(bus))
    info = client.get("/api/office").json()
    assert info["title"] == info["hq_name"] == "Acme <HQ>"
    page = client.get("/").text
    assert "<h1>Acme &lt;HQ&gt;</h1>" in page and "{{HQ_NAME}}" not in page
    unnamed = TestClient(create_app(_bus(tmp_path))).get("/api/office").json()
    assert unnamed["title"] == "xqo" and unnamed["hq_name"] == ""
    for bad in ("x" * 81, "two\nlines", 7):
        with pytest.raises(ValueError):
            hq_name(bad)


def test_overall_usage_is_counted_and_kept(tmp_path):
    b = mock_bus(tmp_path)
    client = TestClient(create_app(b))
    assert client.get("/api/usage").json()["tokens"] == 0
    t = asyncio.run(b.create("Build fib"))
    run = {"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 50, "cost_usd": 0.0123, "turns": 2}
    b.record_usage(t, "planner", run)  # every finished run of an agent adds to it
    b.record_usage(t, "planner", run)
    got = client.get("/api/usage").json()
    assert got["tokens"] == 2500 and got["input_tokens"] == 2000 and got["sessions"] == 2 and got["cost_usd"] == pytest.approx(0.0246)
    assert json.loads((tmp_path / "artifacts" / "usage.json").read_text())["tokens"] == 2500
    assert UsageTotals(tmp_path / "artifacts").report()["tokens"] == 2500  # a restart (or deleted tasks) keeps it
    # the first time, it starts from the tasks the server knows
    (tmp_path / "artifacts" / "usage.json").unlink()
    again = UsageTotals(tmp_path / "artifacts", b.tasks.values()).report()
    assert again["tokens"] == 2500 and again["sessions"] == 2 and again["cost_usd"] == pytest.approx(0.0246)
    (tmp_path / "artifacts" / "usage.json").write_text("{broken")
    assert UsageTotals(tmp_path / "artifacts").report()["tokens"] == 0


def test_a_new_task_is_acknowledged_at_once(tmp_path):
    b = mock_bus(tmp_path)
    t = asyncio.run(b.create("Build fib"))
    kinds = [(e["kind"], e["sender"]) for e in b.history]
    assert kinds[:2] == [("ack", "planner"), ("task", "user")]  # before its title and its first model call
    ack = b.history[0]
    assert ack["task_id"] == t.id and ack["recipient"] == "user" and "Got it" in ack["content"]
    assert [e["kind"] for e in b.task_events(t.id)][0] == "task"  # replay and forks skip it


def test_office_header_bubbles_done_and_no_edit_without_the_flag(browser):  # noqa: F811
    srv, _ = browser
    pg = _open(browser, "?view=3d")
    if pg.evaluate("window.office.r.kind") != "3d":
        pytest.skip("no WebGL in this browser")
    pg.wait_for_function("document.querySelector('#usage').textContent.startsWith('Σ 0 tokens')")
    assert pg.text_content("#title") == "xqo" and pg.title() == "xqo · Office"
    assert pg.query_selector("#editgo")  # this fixture runs with --edit
    # department colour on the name labels
    border = pg.evaluate("[...window.office.r.agents.values()][0].label.style.borderColor")
    assert border and border != "transparent"
    # a long message wraps (and stays at least 5 s)
    pg.evaluate("window.office.r.bubble('planner', 'word '.repeat(60), 5)")
    box = pg.evaluate("(() => {const b = window.office.r.agents.get('planner').bub; return {ws: getComputedStyle(b).whiteSpace, w: b.offsetWidth}})()")
    assert box["ws"] == "normal" and box["w"] <= 260
    # a finished task: a ✓ on its agent until you look at it
    pg.evaluate("""fetch('/api/tasks', {method: 'POST', headers: {'Content-Type': 'application/json'},
                   body: JSON.stringify({prompt: 'Say hi', agent: 'planner'})})""")
    pg.wait_for_function("(window.office.unseen.planner || []).length === 1", timeout=20000)
    pg.wait_for_function("window.office.r.agents.get('planner').badge.textContent === '✓'")
    assert pg.evaluate("JSON.parse(localStorage.getItem('office.unseen')).planner.length") == 1
    pg.evaluate("window.office.r.follow('planner')")
    assert pg.evaluate("window.office.r.following") == "planner"
    pg.wait_for_timeout(1500)
    gap = pg.evaluate("(() => {const r = window.office.r, a = r.agents.get('planner'); return Math.hypot(r.controls.target.x - a.grp.position.x, r.controls.target.z - a.grp.position.z)})()")
    assert gap < 0.5
    pg.keyboard.press("Escape")
    assert pg.evaluate("window.office.r.following") is None
    for _ in range(8):  # looking at the agent clears it (it may be walking: aim again)
        xy = pg.evaluate("window.office.r.screenPos('planner')")
        pg.mouse.click(xy["x"], xy["y"])
        if pg.evaluate("!window.office.unseen.planner"):
            break
        pg.wait_for_timeout(400)
    assert pg.evaluate("!window.office.unseen.planner")
    # windows: darker at night, lighter by day
    tones = pg.evaluate("import('/plugins/office/office3d.js').then(m => [3, 7, 12, 19, 23].map(h => m.daylight(new Date(2026, 0, 1, h))))")
    assert tones[0] == 0 and tones[2] == 1 and tones[4] == 0 and 0 < tones[1] < 1 and 0 < tones[3] < 1
    # the 2D view has no Edit button, and switching to 3D does not start Edit mode
    pg2 = _open(browser, "?view=2d&edit=1")
    assert not pg2.query_selector("#editgo")
    pg3 = _open(browser, "?view=3d&edit=1")
    assert not pg3.evaluate("window.office.edit")


def test_no_edit_button_without_the_flag(tmp_path):
    pw = pytest.importorskip("playwright.sync_api")
    from test_office import _Server
    bus = _bus(tmp_path, edit=False)
    with _Server(create_app(bus)) as srv, pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:
            pytest.skip("no chromium available for playwright")
        pg = b.new_page()
        pg.goto(f"{srv.url}/office?view=3d")
        pg.wait_for_function("window.office && window.office.r && window.office.cit.size > 0", timeout=20000)
        assert not pg.query_selector("#editgo") and pg.inner_html("#editbtn") == ""
        b.close()
