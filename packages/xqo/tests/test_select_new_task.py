"""Submitting a task selects it in the Live viewer (and, with departments, resets the filter to all of them)."""
import glob
import socket
import threading
import time

import pytest
from fastapi.testclient import TestClient

from xqo.config import build
from xqo.web import create_app


def app(tmp_path):
    return create_app(build("agents.json", tmp_path / "a", str(tmp_path / "l" / "e.jsonl"), mock=True))


def test_submit_returns_the_new_task(tmp_path):
    with TestClient(app(tmp_path)) as c:
        r = c.post("/api/tasks", json={"prompt": "Build fib"}).json()
        assert r["accepted"] and r["id"] and r["name"]
        assert r["id"] in {t["id"] for t in c.get("/api/tasks").json()}


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_live_viewer_selects_submitted_task(tmp_path):
    pw = pytest.importorskip("playwright.sync_api")
    import uvicorn
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app(tmp_path), host="127.0.0.1", port=port, log_level="error"))
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
            page.goto(f"http://127.0.0.1:{port}/")
            page.wait_for_function("document.querySelectorAll('#deptf option').length > 1")
            # look at an older task through one department, then submit a new one
            first = page.evaluate("""async () => {
                const r = await fetch('/api/tasks', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                                    body: JSON.stringify({prompt: 'first task'})});
                return (await r.json()).id}""")
            page.wait_for_function(f"[...document.querySelectorAll('#filter option')].some(o => o.value === {first!r})")
            page.select_option("#filter", first)
            page.select_option("#deptf", index=1)
            assert page.eval_on_selector("#deptf", "e => e.value")
            with page.expect_response(lambda r: r.url.endswith("/api/tasks") and r.request.method == "POST") as resp:
                page.fill("#prompt", "second task")
                page.click("#send")
            new = resp.value.json()["id"]
            page.wait_for_function(f"document.getElementById('filter').value === {new!r}")
            res = page.evaluate("({dept: document.getElementById('deptf').value, prompt: document.getElementById('prompt').value})")
            browser.close()
    finally:
        server.should_exit = True
        t.join(5)
    assert new != first
    assert res == {"dept": "", "prompt": ""}
