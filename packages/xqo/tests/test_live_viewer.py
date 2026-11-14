"""Live viewer auto-scroll and line numbers in the source viewer, checked in a real browser when one is available."""
import glob
import socket
import threading
import time
from pathlib import Path

import pytest

from xqo.config import build
from xqo.web import create_app

STATIC = Path(__file__).resolve().parent.parent / "xqo" / "static"


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
    app = create_app(build("agents.json", tmp_path / "a", str(tmp_path / "l" / "e.jsonl"), mock=True))
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        with pw.sync_playwright() as p:
            browser = None
            for exe in (None, *sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome"))):
                try:
                    browser = p.chromium.launch(executable_path=exe)
                    break
                except Exception:
                    continue
            if browser is None:
                pytest.skip("no chromium available for playwright")
            pg = browser.new_page()
            pg.goto(f"http://127.0.0.1:{port}/")
            pg.wait_for_function("document.querySelectorAll('#entry option').length > 0")
            yield pg
            browser.close()
    finally:
        server.should_exit = True
        t.join(5)


GROW = """async () => {  // make the selected task's tree taller, then let the observers run
    const n = document.querySelector('#taskinfo .tnode'), d = document.createElement('div');
    d.style.height = '400px'; n.appendChild(d);
    await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
    const b = document.getElementById('taskinfo'); return {top: b.scrollTop, bottom: atBottom(b)}}"""
SCROLL = """async (y) => {const b = document.getElementById('taskinfo'); b.scrollTop = y;
    await new Promise(r => setTimeout(r, 100)); return b.scrollTop}"""


def test_live_viewer_follows_until_scrolled_up(page):
    page.fill("#prompt", "Build fib")
    page.click("#send")
    page.wait_for_function("document.getElementById('filter').value && document.querySelector('#taskinfo .tnode')")
    page.evaluate("document.getElementById('treesec').style.height = '160px'")
    assert page.evaluate(GROW)["bottom"]                      # new output: scrolled to the end
    page.evaluate(SCROLL, 0)                                  # the user scrolls up ...
    assert page.evaluate(GROW) == {"top": 0, "bottom": False}  # ... and new output leaves them there
    page.evaluate("loadTask()")
    page.wait_for_timeout(300)
    assert page.evaluate("document.getElementById('taskinfo').scrollTop") == 0
    page.evaluate(SCROLL, 10 ** 6)                            # back at the bottom ...
    assert page.evaluate(GROW)["bottom"] is False             # ... not following yet
    page.evaluate(SCROLL, 10 ** 6)
    page.wait_for_timeout(1700)                               # stayed at the bottom for a while: following again
    assert page.evaluate(GROW)["bottom"]


FEED = """async (n) => {  // n events from the server, as the page receives them live
    for (let i = 0; i < n; i++) onEvent({id: 'x' + Math.random(), kind: 'broadcast', sender: 'tester', ts: new Date().toISOString(),
        content: 'line ' + i + '\\n'.repeat(3), task_id: '', root_task_id: '', meta: {}}, true);
    await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
    const b = document.getElementById('events'), last = b.lastElementChild;
    return {bottom: atBottom(b), top: b.scrollTop, last: last && last.textContent.includes('line ' + (n - 1))}}"""
FSCROLL = """async (y) => {const b = document.getElementById('events'); b.scrollTop = y;
    await new Promise(r => setTimeout(r, 100)); return b.scrollTop}"""


def test_message_flow_appends_at_the_bottom_and_follows(page):
    page.evaluate("(s => {s.style.flex = 'none'; s.style.height = '200px'})(document.getElementById('events').parentElement)")
    r = page.evaluate(FEED, 40)
    assert r["last"] and r["bottom"]                          # newest is the last entry, scrolled into view
    page.evaluate(FSCROLL, 0)                                 # the user scrolls up ...
    r = page.evaluate(FEED, 3)
    assert r["last"] and r["top"] == 0 and not r["bottom"]    # ... new messages leave them there
    page.evaluate(FSCROLL, 10 ** 6)                           # back at the bottom, but only briefly
    page.evaluate(FSCROLL, 0)
    page.wait_for_timeout(1700)
    assert not page.evaluate(FEED, 2)["bottom"]               # left again before the delay: still not following
    page.evaluate(FSCROLL, 10 ** 6)
    page.wait_for_timeout(1700)                               # stayed at the bottom: following again
    assert page.evaluate(FEED, 2)["bottom"]


def test_vertical_resizer_drag(page):
    h = page.locator('.vres[data-key="M1"]')
    box = h.bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    h0 = page.evaluate("document.getElementById('treesec').getBoundingClientRect().height")
    page.mouse.move(x, y - 10)                                # the grab area is taller than the 12px handle
    page.mouse.down()
    assert page.evaluate("document.body.classList.contains('resizing')")
    for i in range(1, 11):                                    # far outside the handle: pointer capture keeps the drag
        page.mouse.move(x + 200, y - 10 + i * 15)
    page.mouse.up()
    h1 = page.evaluate("document.getElementById('treesec').getBoundingClientRect().height")
    assert abs(h1 - (h0 + 150)) < 3
    assert not page.evaluate("document.body.classList.contains('resizing') || document.querySelector('.vres.drag') !== null")
    page.mouse.move(x, y + 200)                               # released: hovering no longer resizes
    page.mouse.move(x, y + 100)
    assert page.evaluate("document.getElementById('treesec').getBoundingClientRect().height") == h1
    page.mouse.move(x, y + 150)                               # a cancelled drag cleans up too
    page.mouse.down()
    page.evaluate("window.dispatchEvent(new Event('blur'))")
    assert not page.evaluate("document.body.classList.contains('resizing')")
    page.mouse.up()
    col = page.evaluate("(() => {const c = document.getElementById('colM'); return {h: c.clientHeight, s: c.scrollHeight}})()")
    hy = page.locator('.vres[data-key="M1"]').bounding_box()
    page.mouse.move(x, hy["y"] + 6)
    page.mouse.down()
    page.mouse.move(x, hy["y"] + 3000)                        # dragging past the end stops at the space that is free
    page.mouse.up()
    assert page.evaluate("document.getElementById('colM').scrollHeight <= document.getElementById('colM').clientHeight + 1")


def test_source_viewer_has_line_numbers(page):
    assert "src=numbered(" in "".join(f.read_text() for f in (STATIC / "js").glob("*.js"))
    res = page.evaluate("""() => {const box = document.createElement('div'); box.id = 'viewer';
        box.innerHTML = numbered('a = 1\\nb = 2\\nc = 3\\n', highlighted('a = 1\\nb = 2\\nc = 3\\n', 'x.py', ''));
        document.body.appendChild(box);
        const ln = box.querySelector('pre.ln'), code = box.querySelector('pre:not(.ln) code');
        return {ln: ln.textContent, hl: !!code.querySelector('span'), same: ln.getBoundingClientRect().top === code.parentElement.getBoundingClientRect().top,
                single: numbered('x', 'x').includes('>1</pre>')}}""")
    assert res == {"ln": "1\n2\n3", "hl": True, "same": True, "single": True}
