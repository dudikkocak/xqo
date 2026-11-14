"""xqo-sqlite: the storage contract, restarts, backups, config checks and how xqo finds it."""
import json

import pytest

from xqo.config import build
from xqo.store import open_store
from xqo.testing import check_event_store, check_restart_keeps_tasks, event
from xqo_sqlite import SqliteStore


def test_contract(tmp_path):
    check_event_store(SqliteStore(tmp_path / "events.db"))


def test_all_tasks_survive_a_restart(tmp_path):
    again = check_restart_keeps_tasks(tmp_path, {"backend": "sqlite"})
    assert again.store.backend == "sqlite" and again.store.path == tmp_path / "logs" / "events.db"


def test_found_as_a_plugin_and_not_missing(tmp_path):
    cfg = tmp_path / "agents.json"
    cfg.write_text(json.dumps({"agents": [{"name": "planner", "skills": ["planning"]}],
                               "storage": {"backend": "sqlite"}}))
    bus = build(cfg, tmp_path / "a", str(tmp_path / "logs" / "events.jsonl"), mock=True, departments="none")
    st = bus.plugins.states["sqlite"]
    assert st.state != "missing" and st.source == "xqo_sqlite:SqlitePlugin"
    assert bus.store.backend == "sqlite"


def test_file_and_backup_copy(tmp_path):
    build_dir = tmp_path / "logs"
    from xqo_sqlite import SqlitePlugin
    SqlitePlugin.register()
    s = open_store({"backend": "sqlite"}, build_dir / "events.jsonl")
    assert s.path == build_dir / "events.db"
    s.append(event("task", "r1"))
    files = s.backup_files(tmp_path / "scratch")
    copy = SqliteStore(files["events.db"])
    assert [d["kind"] for d in copy.iter_all()] == ["task"]


def test_bad_config_and_bad_file_stop_startup(tmp_path):
    from xqo_sqlite import SqlitePlugin
    SqlitePlugin.register()
    with pytest.raises(ValueError, match="unknown setting.*for sqlite"):
        open_store({"backend": "sqlite", "table": "x"}, tmp_path / "events.jsonl")
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"this is not a database" * 100)
    with pytest.raises(ValueError, match="storage: can't open"):
        SqliteStore(bad)


def test_env_picks_sqlite(tmp_path, monkeypatch):
    from xqo_sqlite import SqlitePlugin
    SqlitePlugin.register()
    monkeypatch.setenv("XQO_STORAGE", "sqlite")
    assert open_store({"backend": "jsonl"}, tmp_path / "events.jsonl").backend == "sqlite"
