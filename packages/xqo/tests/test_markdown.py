"""Dashboard Markdown rendering: bundled assets, and (when a browser is available) the renderer itself."""
import glob
import re
import socket
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from xqo.config import build
from xqo.web import create_app

STATIC = Path(__file__).resolve().parent.parent / "xqo" / "static"


def app(tmp_path):
    return create_app(build("agents.json", tmp_path / "a", str(tmp_path / "l" / "e.jsonl"), mock=True))


def test_assets_are_local_and_served(tmp_path):
    html = (STATIC / "index.html").read_text()
    refs = re.findall(r'<(?:script|link)[^>]+(?:src|href)="([^"]+)"', html)
    assert refs and all(not r.startswith("http") for r in refs if not r.startswith("data:")), refs
    with TestClient(app(tmp_path)) as c:
        for r in refs:
            if r.startswith("/static/"):
                assert c.get(r).status_code == 200, r
        assert c.get("/static/vendor/mermaid.min.js").status_code == 200
        assert c.get("/static/vendor/fonts/KaTeX_Main-Regular.woff2").status_code == 200
        assert c.get("/static/../web.py").status_code == 404


def test_package_data_includes_vendor():
    py = (STATIC.parent.parent / "pyproject.toml").read_text()
    assert "static/vendor/*" in py and "static/vendor/fonts/*" in py


SAMPLE = r"""# Title
Inline $a^2+b^2=c^2$, \(x\), prices $5 and $10, broken $\frac{1}{$.

$$
\int_0^1 x\,dx
$$

- [x] done
- [ ] todo

| a | b |
|---|---|
| 1 | ~~2~~ |

```python src/x.py
print("hi")
```

```mermaid
flowchart LR
  a --> b
```

Note[^1] <script>window.pwned=1</script> <img src=x onerror="window.pwned=1"> [x](javascript:window.pwned=1)

[^1]: foot
"""


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_renderer_in_browser(tmp_path):
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
            res = page.evaluate("""async (src) => {
                const box = document.createElement('div'); box.innerHTML = MD.render(src);
                document.body.prepend(box); MD.enhance(box);  // on screen: diagrams render lazily
                for (let i = 0; i < 200 && !box.querySelector('.mmd-out svg, .mmd-out .math-err'); i++)
                    await new Promise(r => setTimeout(r, 50));
                return {katex: box.querySelectorAll('.katex').length, display: box.querySelectorAll('.math-block .katex-display').length,
                        err: [...box.querySelectorAll('.math-err')].map(e => e.textContent), text: box.textContent,
                        tasks: box.querySelectorAll('.task-list-item input[type=checkbox]').length,
                        table: !!box.querySelector('table s'), code: !!box.querySelector('.codeblk .cb-copy'),
                        lang: box.querySelector('.cb-lang').textContent, hl: !!box.querySelector('pre code .hljs-built_in, pre code .hljs-string'),
                        svg: !!box.querySelector('.mmd-out svg'), foot: !!box.querySelector('.footnotes'),
                        scripts: box.querySelectorAll('script').length, onerr: box.querySelectorAll('[onerror]').length,
                        js: box.querySelectorAll('a[href^="javascript"]').length, pwned: !!window.pwned,
                        toc: MD.toc(box, 1).includes('Title')};
            }""", SAMPLE)
            browser.close()
    finally:
        server.should_exit = True
        t.join(5)
    assert res["katex"] >= 3 and res["display"] == 1
    assert res["err"] == ["$\\frac{1}{$"] and "$5 and $10" in res["text"]
    assert res["tasks"] == 2 and res["table"] and res["code"] and res["lang"] == "python src/x.py" and res["hl"]
    assert res["svg"] and res["foot"] and res["toc"]
    assert res["scripts"] == 0 and res["onerr"] == 0 and res["js"] == 0 and not res["pwned"]
