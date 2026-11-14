"""Talking to a running xqo server from the command line (core commands and plugin commands)."""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request


def api(url: str, method: str, path: str, body: dict | None = None) -> dict | list:
    """Call a running server. Raises ConnectionError when nothing answers, RuntimeError on API errors."""
    req = urllib.request.Request(url.rstrip("/") + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("detail")
        except ValueError:
            detail = e.reason
        raise RuntimeError(f"{e.code}: {detail}") from None
    except (urllib.error.URLError, OSError) as e:
        raise ConnectionError(str(e)) from None


def call(url: str, method: str, path: str, body: dict | None = None, offline: str = ""):
    """api() for CLI commands: exits with the server's message, or `offline` (default: no server answering)."""
    try:
        return api(url, method, path, body)
    except RuntimeError as e:
        sys.exit(str(e))
    except ConnectionError:
        sys.exit(offline or f"no server answering at {url}")
