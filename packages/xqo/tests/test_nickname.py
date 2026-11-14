"""Nicknames: display names for agents and the user node. The id (agent "name", "user") never changes."""
import glob
import json
import shutil
import socket
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from xqo.branding import resolve_nickname
from xqo.config import build
from xqo.web import create_app

ROOT = Path(__file__).resolve().parent.parent


def write_cfg(tmp_path, nickname="Alex", agent_nick="Plan B", dept_nick="Cody"):
    cfg = json.loads((ROOT / "agents.json").read_text())
    if nickname is not None:
        cfg["user_nickname"] = nickname
    cfg["agents"][0]["nickname"] = agent_nick
    cfg["agents"][0]["avatar"] = "🧪"
    shutil.copytree(ROOT / "departments", tmp_path / "departments")
    f = tmp_path / "departments" / "engineering.json"
    d = json.loads(f.read_text())
    d["agents"][1]["nickname"] = dept_nick
    f.write_text(json.dumps(d))
    p = tmp_path / "agents.json"
    p.write_text(json.dumps(cfg))
    return p, cfg["agents"][0]["name"], d["agents"][1]["name"]


def make(tmp_path, **kw):
    p, first, member = write_cfg(tmp_path, **kw)
    return build(p, tmp_path / "a", str(tmp_path / "l" / "e.jsonl"), mock=True), first, member


def test_resolve_nickname():
    assert resolve_nickname(None, "x") is None and resolve_nickname("", "x") is None
    assert resolve_nickname("  Ada  ", "x") == "Ada"
    assert resolve_nickname("Zoë 🧑‍💻", "x") == "Zoë 🧑‍💻"
    for bad in ("a\nb", "   ", "x" * 41, 5, ["a"], "tab\there"):
        with pytest.raises(ValueError, match="nickname"):
            resolve_nickname(bad, "agent 'a'")
    assert resolve_nickname("x" * 40, "x") == "x" * 40


def test_agents_and_user_carry_their_nicknames(tmp_path):
    b, first, member = make(tmp_path)
    c = TestClient(create_app(b))
    assert c.get("/api/config").json()["user_nickname"] == "Alex"
    by = {a["name"]: a for a in c.get("/api/agents").json()}
    assert by[first]["nickname"] == "Plan B" and by[member]["nickname"] == "Cody"  # HQ and department agents
    assert by[first]["name"] == first  # the id is untouched
    assert all(a["nickname"] is None for n, a in by.items() if n not in (first, member))


def test_no_nicknames_by_default(tmp_path):
    b = build(ROOT / "agents.json", tmp_path / "a", str(tmp_path / "l" / "e.jsonl"), mock=True)
    c = TestClient(create_app(b))
    assert c.get("/api/config").json()["user_nickname"] is None
    assert all(a["nickname"] is None for a in c.get("/api/agents").json())


@pytest.mark.parametrize("where", ["user", "agent"])
def test_bad_nickname_stops_startup_with_a_clear_message(tmp_path, where):
    p, first, _ = write_cfg(tmp_path)
    cfg = json.loads(p.read_text())
    if where == "user":
        cfg["user_nickname"] = "x" * 41
    else:
        cfg["agents"][0]["nickname"] = "two\nlines"
    p.write_text(json.dumps(cfg))
    with pytest.raises(ValueError, match="nickname must be one line"):
        build(p, tmp_path / "a", str(tmp_path / "l" / "e.jsonl"), mock=True)


def test_ids_still_drive_events_and_routing(tmp_path):
    """A nickname is display only: events and the task assignee keep the real name, '@nickname' is not an id."""
    b, first, _ = make(tmp_path)
    c = TestClient(create_app(b))
    r = c.post("/api/tasks", json={"prompt": "hello", "agent": first})
    assert r.status_code == 200 and b.tasks[r.json()["id"]].assignee == first
    assert c.post("/api/tasks", json={"prompt": "hello", "agent": "nobody"}).status_code == 404


def test_people_can_address_an_agent_by_nickname(tmp_path):
    """Entry points for people accept the id or the nickname (case-insensitive) and store the id."""
    b, first, member = make(tmp_path)
    c = TestClient(create_app(b))
    for ref in ("Plan B", "plan b", "@Plan B", first):
        r = c.post("/api/tasks", json={"prompt": "hello", "agent": ref})
        assert r.status_code == 200 and b.tasks[r.json()["id"]].assignee == first, ref
    assert b.agent_id("Cody") == member and b.agent_id(member) == member and b.agent_id("nobody") is None
    assert b.agent_id("Alex") is None  # the user node's nickname is not an agent
    tid = c.post("/api/tasks", json={"prompt": "hello", "agent": first}).json()["id"]
    r = c.post(f"/api/tasks/{tid}/chat", json={"text": "hi", "kind": "ask", "to": "Cody"})
    assert r.status_code == 200 and b.chat.resolve("Cody") == member
    r = c.post(f"/api/tasks/{tid}/chat", json={"text": "hi", "kind": "ask", "to": "nobody"})
    assert r.status_code in (400, 404, 422) and f"{first} (Plan B)" in r.text


@pytest.mark.parametrize("nick", ["engineering", "HQ", "user", "@MEMBER"])
def test_nicknames_may_not_shadow_ids_departments_or_reserved_names(tmp_path, nick):
    p, first, member = write_cfg(tmp_path, nickname=None)
    cfg = json.loads(p.read_text())
    cfg["agents"][0]["nickname"] = member.upper() if nick == "@MEMBER" else nick  # another agent's id, any case
    p.write_text(json.dumps(cfg))
    with pytest.raises(ValueError, match="clashes"):
        build(p, tmp_path / "a", str(tmp_path / "l" / "e.jsonl"), mock=True)


@pytest.mark.parametrize("second", ["Cody", "cody", " CODY "])
def test_two_agents_cannot_share_a_nickname(tmp_path, second):
    p, first, member = write_cfg(tmp_path)  # the department agent is "Cody"
    cfg = json.loads(p.read_text())
    cfg["agents"][0]["nickname"] = second
    p.write_text(json.dumps(cfg))
    with pytest.raises(ValueError, match="used by both"):
        build(p, tmp_path / "a", str(tmp_path / "l" / "e.jsonl"), mock=True)


def test_user_nickname_cannot_equal_an_agent_nickname(tmp_path):
    p, first, _ = write_cfg(tmp_path, nickname="plan b")
    with pytest.raises(ValueError, match="used by both"):
        build(p, tmp_path / "a", str(tmp_path / "l" / "e.jsonl"), mock=True)


def test_an_agent_may_repeat_its_own_id_as_nickname(tmp_path):
    p, first, _ = write_cfg(tmp_path, agent_nick=None)
    cfg = json.loads(p.read_text())
    cfg["agents"][0]["nickname"] = first.upper()
    p.write_text(json.dumps(cfg))
    build(p, tmp_path / "a", str(tmp_path / "l" / "e.jsonl"), mock=True)


# ---------------------------------------------------------------- in a real browser
def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def page(tmp_path):
    pw = pytest.importorskip("playwright.sync_api")
    import uvicorn
    p, first, member = write_cfg(tmp_path)
    app = create_app(build(p, tmp_path / "a", str(tmp_path / "l" / "e.jsonl"), mock=True))
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        with pw.sync_playwright() as pwr:
            browser = None
            for exe in (None, *sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome"))):
                try:
                    browser = pwr.chromium.launch(executable_path=exe)
                    break
                except Exception:
                    continue
            if browser is None:
                pytest.skip("no chromium available for playwright")
            pg = browser.new_page()
            pg.goto(f"http://127.0.0.1:{port}/")
            pg.wait_for_function("document.querySelectorAll('#entry option').length > 0 && state.userNickname")
            yield pg, first, member
            browser.close()
    finally:
        server.should_exit = True
        t.join(5)


def test_dashboard_shows_nicknames_and_keeps_ids(page):
    pg, first, member = page
    labels = pg.evaluate("[...document.querySelectorAll('#graph .gnode')].map(g => [g.dataset.n, g.querySelector('.label').textContent])")
    by = dict(labels)
    assert by["user"] == "Alex" and by[first] == "Plan B" and by[member] == "Cody"
    # the real id stays available as a tooltip and as the data attribute
    assert pg.evaluate(f"document.querySelector('#graph .gnode[data-n=\"{first}\"] > title').textContent") == first
    # the entry selector shows the nickname but submits the id
    opt = pg.evaluate(f"(() => {{const o = [...document.querySelectorAll('#entry option')].find(o => o.value === '{first}'); return [o.value, o.textContent]}})()")
    assert opt == [first, "Plan B"]
    assert pg.evaluate("nick('user')") == "Alex" and pg.evaluate("nick('nobody')") == "nobody"
    rows = pg.evaluate("[...document.querySelectorAll('#agents .agent b')].map(b => b.textContent)")
    assert "Plan B" in rows and "Cody" in rows and first not in rows


@pytest.mark.parametrize("view", ["2d", "3d"])
def test_office_shows_nicknames_in_2d_and_3d(page, view):
    pg, first, member = page
    base = pg.url.rstrip("/")
    pg.add_init_script("window.__texts = []; const f = CanvasRenderingContext2D.prototype.fillText;"
                       "CanvasRenderingContext2D.prototype.fillText = function (t, ...r) { window.__texts.push(String(t)); return f.call(this, t, ...r) }")
    pg.goto(f"{base}/office?view={view}")
    if view == "3d":
        try:
            pg.wait_for_function("[...document.querySelectorAll('.o3-label')].some(e => e.textContent.includes('Plan B'))", timeout=15000)
        except Exception:
            pytest.skip("WebGL is not available in this browser")
        texts = pg.evaluate("[...document.querySelectorAll('.o3-label')].map(e => e.textContent)")
    else:
        pg.wait_for_function("window.__texts.some(t => t.includes('Plan B'))", timeout=15000)
        texts = pg.evaluate("window.__texts")
    joined = " | ".join(texts)
    assert "Plan B" in joined and "Cody" in joined
    assert first not in [t.replace(" ★", "") for t in texts]  # the id is not the label


def test_planners_world_shows_the_nickname_after_the_hq_name(page):
    pg, first, member = page
    pg.add_init_script("window.__texts = []; const f = CanvasRenderingContext2D.prototype.fillText;"
                       "CanvasRenderingContext2D.prototype.fillText = function (t, ...r) { window.__texts.push(String(t)); return f.call(this, t, ...r) }")
    pg.goto(f"{pg.url.rstrip('/')}/office?world=planners&view=2d")
    pg.wait_for_function("window.__texts.some(t => t.endsWith('/Plan B'))", timeout=15000)
    assert not any(t.endswith(f"/{first}") for t in pg.evaluate("window.__texts"))


def test_office_panel_names_the_agent_by_nickname_with_the_id_below(page):
    """2D office: click the agent (found through its name tag) and read the side panel."""
    pg, first, member = page
    pg.add_init_script("window.__tags = []; const f = CanvasRenderingContext2D.prototype.fillText;"
                       "CanvasRenderingContext2D.prototype.fillText = function (t, x, y, ...r) {"
                       " if (String(t).startsWith('Plan B')) window.__tags = [x, y, this.canvas.width]; return f.call(this, t, x, y, ...r) }")
    pg.goto(f"{pg.url.rstrip('/')}/office?view=2d")
    pg.wait_for_function("window.__tags.length === 3", timeout=15000)
    for dy in (14, 22, 30, 6):  # the sprite is just below its name tag
        x, y, w = pg.evaluate("window.__tags")
        left, top, width = pg.evaluate("(() => { const r = document.querySelector('#world canvas').getBoundingClientRect(); return [r.left, r.top, r.width] })()")
        pg.mouse.click(left + x * width / w, top + (y + dy) * width / w)
        if pg.locator("#phead h3").count():
            break
    _check_panel(pg, first)


def test_office_panel_names_the_agent_by_nickname_in_3d(page):
    pg, first, member = page
    pg.goto(f"{pg.url.rstrip('/')}/office?view=3d")
    try:
        pg.wait_for_function("[...document.querySelectorAll('.o3-label')].some(e => e.textContent.includes('Plan B'))", timeout=15000)
    except Exception:
        pytest.skip("WebGL is not available in this browser")
    for dy in (24, 36, 48, 12):  # the character stands below its name tag
        b = pg.evaluate("(() => { const r = [...document.querySelectorAll('.o3-label')].find(e => e.textContent.includes('Plan B')).getBoundingClientRect(); return [r.left + r.width / 2, r.bottom] })()")
        pg.mouse.click(b[0], b[1] + dy)
        if pg.locator("#phead h3").count():
            break
    _check_panel(pg, first)


def _check_panel(pg, first):
    pg.wait_for_selector("#phead h3", timeout=5000)
    assert pg.locator("#phead h3").inner_text().startswith("Plan B")         # the nickname is the title
    assert pg.locator("#phead .agentid").inner_text() == first               # the id sits below it
    # the avatar is right-aligned, just left of the close button
    av, close, chip = pg.evaluate("""(() => { const r = q => { const e = document.querySelector('#phead ' + q); if (!e) return null;
        const b = e.getBoundingClientRect(); return {x: b.x, w: b.width} }; return [r('.avi'), r('#pclose'), r('.chip')] })()""")
    assert pg.locator("#phead .avi").inner_text() == "🧪"
    assert chip["x"] + chip["w"] <= av["x"] and av["x"] + av["w"] <= close["x"] and close["x"] - (av["x"] + av["w"]) < 24
    panel = pg.locator("#panel").inner_text().lower()
    assert "tasks of plan b" in panel and f"tasks of {first}" not in panel


def test_office_endpoint_sends_avatars_of_the_planners(tmp_path):
    p, first, member = write_cfg(tmp_path)
    d = tmp_path / "departments" / "engineering.json"
    dd = json.loads(d.read_text())
    (tmp_path / "me.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    dd["agents"][1]["avatar"] = "../me.svg"
    d.write_text(json.dumps(dd))
    cfg = json.loads(p.read_text())
    cfg["office"] = {"planners": [first, member]}
    p.write_text(json.dumps(cfg))
    b = build(p, tmp_path / "a", str(tmp_path / "l" / "e.jsonl"), mock=True)
    o = TestClient(create_app(b)).get("/api/office").json()
    assert o["avatars"][first] == {"kind": "text", "text": "🧪"}
    assert o["avatars"][member]["kind"] == "image" and o["avatars"][member]["url"].startswith(f"/api/avatars/{member}?v=")
    assert "path" not in json.dumps(o)  # no local file paths leave the server
