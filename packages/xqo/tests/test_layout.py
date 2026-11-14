"""Graph layout picker: default from agents.json, validated at startup."""
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from xqo.config import GRAPH_LAYOUTS, build
from xqo.web import create_app

ROOT = Path(__file__).resolve().parent.parent


def _cfg(tmp_path, layout):
    cfg = json.loads((ROOT / "agents.json").read_text())
    cfg["graph_layout"] = layout
    p = tmp_path / "agents.json"
    p.write_text(json.dumps(cfg))
    if (ROOT / "departments").is_dir() and not (tmp_path / "departments").exists():
        shutil.copytree(ROOT / "departments", tmp_path / "departments")
    return p


def test_graph_layout_default(tmp_path):
    assert {"ring", "grid", "star"} <= set(GRAPH_LAYOUTS)
    b = build(_cfg(tmp_path, "grid"), tmp_path / "a", str(tmp_path / "e.jsonl"), mock=True)
    with TestClient(create_app(b)) as c:
        assert c.get("/api/config").json()["graph_layout"] == "grid"
        html = c.get("/").text + c.get("/static/js/agents.js").text
        assert 'id="glayout"' in html and "function arrange(" in html
    with pytest.raises(ValueError, match="graph_layout"):
        build(_cfg(tmp_path, "spiral"), tmp_path / "b", str(tmp_path / "f.jsonl"), mock=True)
