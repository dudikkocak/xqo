"""Workspace and attachment limits from "limits" in agents.json (environment variables win)."""
import base64
import json
from pathlib import Path

import pytest

from xqo import artifacts, attachments
from xqo.config import build
from xqo.limits import apply_limits
from xqo.patching import PatchError

ROOT = Path(__file__).resolve().parent.parent
PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 3_000_000


@pytest.fixture(autouse=True)
def defaults():
    yield
    apply_limits(None)  # other tests expect the built-in limits


def _build(tmp_path, limits):
    cfg = json.loads((ROOT / "agents.json").read_text())
    cfg["limits"] = limits
    cfg["git"] = {"enabled": False}
    (tmp_path / "agents.json").write_text(json.dumps(cfg))
    return build(tmp_path / "agents.json", tmp_path / "art", str(tmp_path / "e.jsonl"), mock=True)


def test_defaults():
    v, warnings = apply_limits(None)
    assert v == {"workspace_file_mb": 5, "image_mb": 5, "pdf_mb": 20, "text_attachment_mb": 2,
                 "attachments_total_mb": 24, "attachments_max_files": 20}
    assert not warnings and artifacts.MAX_FILE_BYTES == 5_000_000 and attachments.MAX_TOTAL == 24_000_000


def test_workspace_file_limit(tmp_path):
    bus = _build(tmp_path, {"workspace_file_mb": 0.001})
    with pytest.raises(PatchError, match="limits.workspace_file_mb"):
        bus.artifacts.write_file("t", "big.txt", "x" * 2000, "t", "coder")
    bus = _build(tmp_path, {"workspace_file_mb": 12})
    bus.artifacts.write_file("t", "big.txt", "x" * 6_000_000, "t", "coder")  # over the old 5 MB


def test_attachment_limits_and_env(tmp_path, monkeypatch):
    _build(tmp_path, {"image_mb": 2, "attachments_max_files": 1})
    with pytest.raises(ValueError, match="image files are limited to 2 MB"):
        attachments.validate([("a.png", None, PNG)])
    with pytest.raises(ValueError, match="at most 1"):
        attachments.validate([("a.txt", None, b"a"), ("b.txt", None, b"b")])
    monkeypatch.setenv("XQO_IMAGE_MB", "4")
    _build(tmp_path, {"image_mb": 2})
    assert attachments.validate([("a.png", None, PNG)])[0]["kind"] == "image"


def test_bad_values_and_api_warnings(tmp_path):
    with pytest.raises(ValueError, match="unknown"):
        apply_limits({"image": 1})
    with pytest.raises(ValueError, match="positive"):
        apply_limits({"pdf_mb": 0})
    _, warnings = apply_limits({"image_mb": 10, "attachments_total_mb": 100})
    assert len(warnings) == 2 and all("Claude API" in w for w in warnings)


def test_dashboard_gets_and_enforces_the_limits(tmp_path):
    from fastapi.testclient import TestClient

    from xqo.web import create_app
    bus = _build(tmp_path, {"image_mb": 1})
    c = TestClient(create_app(bus))
    assert c.get("/api/config").json()["limits"]["image_mb"] == 1
    att = {"name": "a.png", "data": base64.b64encode(PNG).decode()}
    r = c.post("/api/tasks", json={"prompt": "look", "attachments": [att]})
    assert r.status_code == 400 and "limited to 1 MB" in r.json()["detail"]
