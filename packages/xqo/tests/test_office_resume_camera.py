"""Resume shows at once (dashboard and office), the office panel offers Resume whenever it is possible, and the 3D
office starts from the camera the user left it at."""
import pytest

from test_office import _open, browser  # noqa: F401 (browser: the fixture)


def _done_task(pg, agent="planner"):
    tid = pg.evaluate(f"""fetch('/api/tasks', {{method: 'POST', headers: {{'Content-Type': 'application/json'}},
                       body: JSON.stringify({{prompt: 'Say hi', agent: '{agent}'}})}}).then(r => r.json()).then(t => t.id)""")
    pg.wait_for_function(f"fetch('/api/tasks/{tid}').then(r => r.json()).then(t => t.status === 'done')", timeout=20000)
    return tid


def _click_agent(pg, agent="planner"):
    for _ in range(8):  # agents walk around when idle: aim again if the click missed
        xy = pg.evaluate(f"window.office.r.screenPos('{agent}')")
        pg.mouse.click(xy["x"], xy["y"])
        try:
            pg.wait_for_selector("#panel h3", timeout=2000)
            return
        except Exception:
            continue
    raise AssertionError("could not click the agent")


def test_office_panel_resumes_a_finished_task_with_instant_feedback(browser):  # noqa: F811
    pg = _open(browser, "?view=2d")
    tid = _done_task(pg)
    _click_agent(pg)
    pg.click(f'#panel [data-task="{tid}"]')
    pg.wait_for_selector('#panel [data-act="resume"]')
    assert not pg.query_selector('#panel [data-act="pause"]')
    # the click shows at once: the button says so and the agent says it in a bubble, before the server answers
    got = pg.evaluate("""(() => {const r = window.office.r, said = []; const f = r.bubble; r.bubble = (id, t, s) => {said.push(t); f(id, t, s)};
        const b = document.querySelector('#panel [data-act="resume"]'); b.click();
        return [b.textContent, b.disabled, said.join(' ')]})()""")
    assert got[0] == "⏳ resuming…" and got[1] is True and "Resuming" in got[2]
    pg.wait_for_function(f"fetch('/api/tasks/{tid}/events').then(r => r.json()).then(es => es.some(e => e.kind === 'task_reopened'))",
                         timeout=10000)


def test_dashboard_resume_shows_at_once(browser):  # noqa: F811
    srv, b = browser
    pg = b.new_page(viewport={"width": 1400, "height": 850})
    pg.goto(srv.url + "/")
    tid = _done_task(pg)
    pg.goto(f"{srv.url}/?task={tid}")
    pg.wait_for_selector("#tresume", state="visible", timeout=10000)
    got = pg.evaluate("""(() => {const b = document.getElementById('tresume'); b.click();
        return [b.textContent, b.disabled, PEND.on && PEND.on.mode,
                document.querySelector('.gnode[data-n="user"] text.stat').textContent]})()""")
    assert got[0] == "⏳ resuming…" and got[1] is True and got[2] == "resume" and got[3] == "resumed · waiting…"
    pg.wait_for_function("!PEND.on", timeout=20000)  # an agent responded
    assert pg.text_content("#tresume") == "▶ resume"


def test_3d_office_starts_from_the_last_camera(browser):  # noqa: F811
    srv, b = browser
    ctx = b.new_context(viewport={"width": 1400, "height": 850})
    pg = ctx.new_page()
    pg.goto(f"{srv.url}/office?view=3d")
    pg.wait_for_function("window.office && window.office.r && window.office.cit.size > 0", timeout=20000)
    if pg.evaluate("window.office.r.kind") != "3d":
        pytest.skip("no WebGL in this browser")
    fitted = pg.evaluate("window.office.r.view()")
    # nothing saved yet: the starting view looks at the office from its front-left corner, from above
    pos, target = fitted["pos"], fitted["target"]
    assert pos[0] < target[0] and pos[1] > target[1] and pos[2] > target[2]
    pg.mouse.move(700, 400)
    pg.mouse.wheel(0, -600)  # zoom in
    pg.wait_for_timeout(2000)  # damping settles, the next save has the final view
    kept = pg.evaluate("window.office.r.view()")
    assert kept["pos"] != fitted["pos"]
    pg.close()
    pg = ctx.new_page()
    pg.goto(f"{srv.url}/office?view=3d")
    pg.wait_for_function("window.office && window.office.r && window.office.cit.size > 0", timeout=20000)
    pg.wait_for_timeout(500)
    now = pg.evaluate("window.office.r.view()")
    assert all(abs(a - b) < 0.05 for a, b in zip(now["pos"] + now["target"], kept["pos"] + kept["target"]))
    assert pg.evaluate("window.office.r.moved")
    # a zoom preset is the last view too: it is saved and restored like any other
    pg.select_option("#zoom", "1")
    pg.wait_for_timeout(1500)
    preset = pg.evaluate("window.office.r.view()")
    saved = pg.evaluate("JSON.parse(localStorage.getItem('office.camera3d.office.local'))")
    assert all(abs(a - b) < 0.01 for a, b in zip(saved["pos"], preset["pos"]))
    ctx.close()
