"""The user's node in the Flow graph pulses as soon as a New task / Ask / Steer is sent, until an agent responds
(its first event on that task), an error, or the timeout."""
import glob
import socket
import threading
import time
from contextlib import contextmanager

import pytest

from xqo.config import build
from xqo.web import create_app

USER = "document.querySelector('.gnode[data-n=\"user\"]')"
SENDING = f"{USER}.classList.contains('sending')"
# hold back the browser's POSTs to a URL (ms, or forever with -1) or fail them, so the pending state can be seen
HOLD = """([part, ms, fail]) => {const f = window.fetch;
  window.fetch = (u, o = {}) => String(u).endsWith(part) && o.method === 'POST'
    ? (fail ? Promise.resolve(new Response(JSON.stringify({detail: 'nope'}), {status: 500}))
            : ms < 0 ? new Promise(() => {}) : new Promise(ok => setTimeout(() => ok(f(u, o)), ms)))
    : f(u, o)}"""


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@contextmanager
def dashboard(tmp_path):
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
            page = browser.new_page()
            page.on("dialog", lambda d: d.accept())
            page.goto(f"http://127.0.0.1:{port}/")
            page.wait_for_function(f"document.querySelectorAll('#entry option').length > 0 && !!{USER}")
            page.wait_for_function("document.getElementById('dot').classList.contains('on')")
            yield page
            browser.close()
    finally:
        server.should_exit = True
        t.join(5)


def _stat(page):
    return page.evaluate(f"{USER}.querySelector('.stat').textContent")


def test_new_task_pulses_until_an_agent_responds(tmp_path):
    with dashboard(tmp_path) as page:
        page.evaluate(HOLD, ["/api/tasks", 1500, False])
        page.fill("#prompt", "Build fib")
        page.click("#send")
        assert page.evaluate(SENDING), "the user node pulses right after the click"
        assert _stat(page) == "submitted · waiting…"
        page.wait_for_function(f"!{SENDING}", timeout=10000)
        # cleared by the agent's first event on the new task: the task is known by then
        assert page.evaluate("[...document.querySelectorAll('#filter option')].some(o => o.value)")
        assert _stat(page) == ""


def test_failed_submit_stops_the_pulse(tmp_path):
    with dashboard(tmp_path) as page:
        page.evaluate(HOLD, ["/api/tasks", 0, True])
        page.fill("#prompt", "Build fib")
        page.click("#send")
        page.wait_for_function(f"!{SENDING}", timeout=5000)
        assert page.evaluate("document.getElementById('prompt').value") == "Build fib"


def test_pulse_times_out_without_a_server_event(tmp_path):
    with dashboard(tmp_path) as page:
        page.evaluate("PEND.ms = 400")
        page.evaluate(HOLD, ["/api/tasks", -1, False])
        page.fill("#prompt", "Build fib")
        page.click("#send")
        assert page.evaluate(SENDING)
        page.wait_for_function(f"!{SENDING}", timeout=5000)


def test_ask_pulses_until_an_agent_responds(tmp_path):
    with dashboard(tmp_path) as page:
        tid = page.evaluate("""async () => (await (await fetch('/api/tasks', {method: 'POST',
            headers: {'Content-Type': 'application/json'}, body: JSON.stringify({prompt: 'first task'})})).json()).id""")
        page.wait_for_function(f"[...document.querySelectorAll('#filter option')].some(o => o.value === {tid!r})")
        page.select_option("#filter", tid)
        page.click(".tab[data-mode='ask']")
        page.evaluate(HOLD, ["/chat", 1500, False])
        page.fill("#prompt", "how is it going?")
        page.click("#send")
        assert page.evaluate(SENDING)
        assert _stat(page) == "asked · waiting…"
        page.wait_for_function(f"!{SENDING}", timeout=10000)
        assert page.evaluate(f"""state.events.some(e => e.kind === 'user_message' && e.root_task_id === {tid!r})""")
