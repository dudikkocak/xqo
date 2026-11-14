"""Task folder paths for the dashboard: host path mapping, SSH and editor links."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from xqo import folders
from xqo.config import build
from xqo.folders import FolderConfig
from xqo.web import create_app


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    for k in ("XQO_PATH_MAP", "XQO_SSH_HOST", "XQO_EDITOR"):
        monkeypatch.delenv(k, raising=False)


def test_path_map_longest_prefix_and_windows_target():
    c = FolderConfig.from_dict({"path_map": {"/data": "/home/me/xqo", "/data/artifacts": "D:\\xqo\\art"}})
    assert c.user_path(Path("/data/logs/x")) == "/home/me/xqo/logs/x"
    assert c.user_path(Path("/data/artifacts/t1/workspace")) == "D:\\xqo\\art\\t1\\workspace"
    assert c.user_path(Path("/database/x")) == "/database/x"  # prefix must end at a path separator
    assert c.user_path(Path("/data")) == "/home/me/xqo"


def test_env_wins_and_string_form(monkeypatch):
    monkeypatch.setenv("XQO_PATH_MAP", "/data=/srv/xqo;/tmp/x=/y")
    monkeypatch.setenv("XQO_EDITOR", "cursor")
    c = FolderConfig.from_dict({"path_map": {"/data": "/ignored"}, "editor": "vscode"})
    assert c.user_path(Path("/data/a")) == "/srv/xqo/a" and c.editor == "cursor"


def test_editor_uris():
    assert FolderConfig().editor_uri("/home/me/my ws") == "vscode://file/home/me/my%20ws"
    assert FolderConfig().editor_uri("C:\\xqo\\ws") == "vscode://file/C:/xqo/ws"
    ssh = FolderConfig(ssh_host="me@box.lan")
    assert ssh.editor_uri("/srv/xqo/t1") == "vscode://vscode-remote/ssh-remote+me@box.lan/srv/xqo/t1"
    assert FolderConfig(editor="none").editor_uri("/x") is None


def test_bad_settings():
    with pytest.raises(ValueError, match="editor"):
        FolderConfig.from_dict({"editor": "emacs"})
    with pytest.raises(ValueError, match="unknown"):
        FolderConfig.from_dict({"host": "x"})
    with pytest.raises(ValueError, match="path_map"):
        FolderConfig.from_dict({"path_map": "/data"})


def test_container_without_mapping_hides_links(monkeypatch, tmp_path):
    monkeypatch.setattr(folders, "in_container", lambda: True)
    d = FolderConfig().describe([("workspace", tmp_path)])
    assert d["folders"][0]["uri"] is None and "XQO_PATH_MAP" in d["hint"]
    d = FolderConfig.from_dict({"path_map": {str(tmp_path): "/host"}}).describe([("workspace", tmp_path)])
    assert d["folders"][0] == {"kind": "workspace", "path": "/host", "exists": True, "uri": "vscode://file/host"}
    assert d["hint"] is None


def test_api(tmp_path, monkeypatch):
    monkeypatch.setattr(folders, "in_container", lambda: False)
    bus = build(artifacts_dir=str(tmp_path / "artifacts"), log_path=str(tmp_path / "ev.jsonl"), mock=True)
    bus.folders = FolderConfig.from_dict({"path_map": {str(tmp_path): "/home/me/xqo"}})
    bus.artifacts.write_file("t1", "a.py", "x = 1\n", "t1", "coder")
    c = TestClient(create_app(bus))
    rows = c.get("/api/folders/t1").json()["folders"]
    assert [r["kind"] for r in rows] == ["task", "workspace", "attachments"]
    assert rows[1]["path"] == "/home/me/xqo/artifacts/t1/workspace" and rows[1]["exists"]
    assert not rows[2]["exists"]
    assert c.get("/api/folders/nope").status_code == 404
