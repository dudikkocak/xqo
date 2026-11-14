"""The event store: jsonl in the base package; other backends come from plugins through register_store."""
import json

import pytest

from xqo.config import build
from xqo import store as store_mod
from xqo.store import JsonlStore, open_store, register_store


def ev(kind, task, root=None, **meta):
    return {"ts": "2026-11-01T00:00:00", "kind": kind, "sender": "a", "recipient": "b", "task_id": task,
            "root_task_id": root or task, "content": kind, "meta": meta}


def test_contract(tmp_path):
    store = JsonlStore(tmp_path / "events.jsonl")
    store.append(ev("task", "r1"))
    store.append(ev("handover", "c1", "r1"))
    store.append(ev("llm", "c1", "r1"))
    store.append(ev("task", "r2"))
    store.append(ev("schedule_run", None, None, schedule_id="s1"))
    store.append(ev("done", "r1"))
    store.append(ev("task_deleted", "r1"))
    assert store.count() == 7
    assert [d["kind"] for d in store.recent(2)] == ["done", "task_deleted"]
    assert [d["kind"] for d in store.seeds()] == ["task", "handover", "task", "done"]
    assert [d["kind"] for d in store.tree("r1")] == ["task", "handover", "llm", "done", "task_deleted"]
    assert [d["meta"]["schedule_id"] for d in store.by_kind_prefix("schedule_")] == ["s1"]
    assert store.by_kind_prefix("sched%") == []
    removed = store.purge("r1", {"r1", "c1"}, {"task_deleted"})
    assert removed == 4
    assert [d["kind"] for d in store.iter_all()] == ["task", "schedule_run", "task_deleted"]
    store.close()


def _cfg(tmp_path, storage):
    p = tmp_path / "agents.json"
    p.write_text(json.dumps({"agents": [{"name": "planner", "skills": ["planning"]}], "storage": storage}))
    return p


def test_all_tasks_survive_a_restart_beyond_the_history_cap(tmp_path, monkeypatch):
    import xqo.bus as busmod
    monkeypatch.setattr(busmod, "HISTORY_LIMIT", 5)
    cfg = _cfg(tmp_path, {"backend": "jsonl"})
    bus = build(cfg, tmp_path / "artifacts", str(tmp_path / "logs" / "events.jsonl"), mock=True, departments="none")
    for i in range(6):  # 6 tasks, 2+ events each: far more than the 5-event cap
        bus.emit(busmod.Event("task", "user", "planner", f"t{i}", None, f"job {i}"))
        bus.emit(busmod.Event("done", "planner", "user", f"t{i}", None, "ok", {"status": "done"}))
    again = build(cfg, tmp_path / "artifacts", str(tmp_path / "logs" / "events.jsonl"), mock=True, departments="none")
    assert {f"t{i}" for i in range(6)} <= set(again.tasks)
    assert len(again.history) == 5 and again.tasks["t0"].status == "done"
    assert again.store.backend == "jsonl"


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
