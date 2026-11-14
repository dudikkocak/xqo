"""Artifacts panel: one collapsible group per task, department workspaces nested, open state kept,
an "active task only" filter, and collapse/expand all."""
import glob
import socket
import threading
import time

import pytest

from xqo.config import build
from xqo.web import create_app


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


GROUPS = """() => [...document.querySelectorAll('#artifacts details.tgrp')].map(d => ({
    k: d.dataset.k, open: d.open, parent: d.parentElement.closest('details.tgrp')?.dataset.k || null}))"""


def test_artifact_groups_collapse_filter_and_persist(tmp_path):
    pw = pytest.importorskip("playwright.sync_api")
    import uvicorn
    bus = build(artifacts_dir=str(tmp_path / "artifacts"), log_path=str(tmp_path / "ev.jsonl"), mock=True)
    bus.artifacts.write_file("task_a", "a.py", "a = 1\n", "task_a", "coder")
    bus.artifacts.write_file("task_a.engineering", "e.py", "e = 1\n", "task_a.engineering", "coder")
    bus.artifacts.write_file("task_b", "b.py", "b = 1\n", "task_b", "coder")
    bus.artifacts.write_meta("task_b", {"name": "task_b : Fix the login bug"})
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(bus), host="127.0.0.1", port=port, log_level="error"))
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
            page.wait_for_function("document.querySelectorAll('#artifacts details.tgrp').length === 3")
            g = {x["k"]: x for x in page.evaluate(GROUPS)}
            # newest task first and open; the older one collapsed; the department nested under its task
            assert page.evaluate(GROUPS)[0]["k"] == "task_b"
            assert g["task_b"]["open"] and not g["task_a"]["open"]
            assert g["task_a.engineering"]["parent"] == "task_a" and g["task_b"]["parent"] is None

            # header: the id, then the title, then the count and tools, each on its own line
            ys = page.evaluate("""() => ['[data-id]', '[data-title]', '[data-git]'].map(q =>
                document.querySelector('details[data-k="task_b"] > summary ' + q).getBoundingClientRect().top)""")
            assert ys[0] < ys[1] < ys[2]
            assert page.text_content('details[data-k="task_b"] > summary [data-id]') == "task_b"
            assert page.text_content('details[data-k="task_b"] > summary [data-title]') == "Fix the login bug"
            # buttons in a group's header do not toggle it
            page.click('details[data-k="task_b"] > summary [data-git]')
            assert page.eval_on_selector('details[data-k="task_b"]', "d => d.open")
            page.wait_for_selector("#dlg #dclose", state="visible")
            page.click("#dclose")
            page.wait_for_selector("#dlg", state="hidden")

            # open/closed survives a reload
            page.click('details[data-k="task_a"] > summary .tname')
            page.click('details[data-k="task_b"] > summary .tname')
            page.reload()
            page.wait_for_function("document.querySelectorAll('#artifacts details.tgrp').length === 3")
            g = {x["k"]: x for x in page.evaluate(GROUPS)}
            assert g["task_a"]["open"] and not g["task_b"]["open"]

            # active task only: the selected task and its department workspace
            page.evaluate("addTask('task_a', 'A'); document.getElementById('filter').value = 'task_a';"
                          " document.getElementById('filter').onchange()")
            page.check("#artactive")
            page.wait_for_function("document.querySelectorAll('#artifacts details.tgrp').length === 2")
            assert sorted(x["k"] for x in page.evaluate(GROUPS)) == ["task_a", "task_a.engineering"]
            assert page.evaluate("JSON.parse(localStorage.getItem('artActive'))") is True

            # collapse all / expand all
            page.click("#artcollapse")
            page.wait_for_function("[...document.querySelectorAll('#artifacts details.tgrp')].every(d => !d.open)")
            page.uncheck("#artactive")
            page.wait_for_function("document.querySelectorAll('#artifacts details.tgrp').length === 3")
            page.click("#artexpand")
            page.wait_for_function("[...document.querySelectorAll('#artifacts details.tgrp')].every(d => d.open)")
            # a file link still opens the viewer
            page.click('details[data-k="task_a.engineering"] .link[data-file="e.py"]')
            page.wait_for_function("document.getElementById('modal').style.display === 'block'")
            browser.close()
    finally:
        server.should_exit = True
        t.join(5)
