"""The event store: jsonl in the base package; other backends come from plugins through register_store."""
import json

import pytest

from xqo.config import build
from xqo import store as store_mod
from xqo.store import JsonlStore, open_store, register_store
from xqo.testing import check_event_store, check_restart_keeps_tasks, event


def ev(kind, task, root=None, **meta):
    return event(kind, task, root, **meta)


def test_contract(tmp_path):
    check_event_store(JsonlStore(tmp_path / "events.jsonl"))


def _cfg(tmp_path, storage):
    p = tmp_path / "agents.json"
    p.write_text(json.dumps({"agents": [{"name": "planner", "skills": ["planning"]}], "storage": storage}))
    return p


def test_all_tasks_survive_a_restart_beyond_the_history_cap(tmp_path):
    assert check_restart_keeps_tasks(tmp_path, {"backend": "jsonl"}).store.backend == "jsonl"


def test_custom_path_and_backup_file(tmp_path):
    s = open_store({"backend": "jsonl", "path": "other.jsonl"}, tmp_path / "logs" / "events.jsonl")
    assert s.path == tmp_path / "logs" / "other.jsonl"
    s.append(ev("task", "r1"))
    assert s.backup_files(tmp_path / "scratch") == {"other.jsonl": s.path}


@pytest.mark.parametrize("backend,pkg", [("sqlite", "xqo-sqlite"), ("postgres", "xqo-postgres")])
def test_plugin_backends_not_installed_stop_startup_with_the_pip_name(tmp_path, backend, pkg):
    with pytest.raises(ValueError, match=f"{backend!r} is not installed \\(pip install {pkg}\\)"):
        open_store({"backend": backend}, tmp_path / "events.jsonl")
    with pytest.raises(ValueError, match="not installed"):
        build(_cfg(tmp_path, {"backend": backend}), tmp_path / "a", str(tmp_path / "l" / "e.jsonl"), mock=True)


def test_bad_storage_config_stops_startup(tmp_path):
    with pytest.raises(ValueError, match="'mongo' is not installed; available: jsonl"):
        open_store({"backend": "mongo"}, tmp_path / "events.jsonl")
    with pytest.raises(ValueError, match="unknown setting"):
        open_store({"backend": "jsonl", "dsn_env": "X"}, tmp_path / "events.jsonl")
    with pytest.raises(ValueError, match="dsn_env"):
        open_store({"backend": "jsonl", "dsn": "postgresql://x"}, tmp_path / "events.jsonl")


def test_env_override_to_a_missing_backend_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("XQO_STORAGE", "sqlite")
    with pytest.raises(ValueError, match="pip install xqo-sqlite"):
        open_store({"backend": "jsonl"}, tmp_path / "events.jsonl")


def test_a_plugin_registers_a_backend(tmp_path, monkeypatch):
    class MemStore(JsonlStore):
        backend = "mem"
    seen = {}

    def factory(cfg, logs):
        seen.update(cfg=cfg, logs=logs)
        return MemStore(logs / "mem.jsonl")
    monkeypatch.setitem(store_mod.STORES, "mem", None)  # restored after the test
    register_store("mem", factory)
    s = open_store({"backend": "mem", "table": "t"}, tmp_path / "logs" / "events.jsonl")
    assert s.backend == "mem" and seen["cfg"]["table"] == "t" and seen["logs"] == tmp_path / "logs"
    monkeypatch.setenv("XQO_STORAGE", "mem")
    assert open_store(None, tmp_path / "logs" / "events.jsonl").backend == "mem"
