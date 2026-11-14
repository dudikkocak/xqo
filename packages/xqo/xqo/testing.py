"""Checks a plugin package runs in its own tests (pytest is not needed to import this module).

    from xqo.testing import check_event_store, check_restart_keeps_tasks
    check_event_store(MyStore(...))          # the contract every storage backend must pass
"""
from __future__ import annotations

import json
from pathlib import Path


def event(kind: str, task: str | None, root: str | None = None, **meta) -> dict:
    """An event dict as the bus writes it (Event.to_dict() plus routing fields)."""
    return {"ts": "2026-11-01T00:00:00", "kind": kind, "sender": "a", "recipient": "b", "task_id": task,
            "root_task_id": root or task, "content": kind, "meta": meta}


def check_event_store(store) -> None:
    """The storage contract: append, counts, recent, seeds, trees, kind prefixes (LIKE wildcards escaped), purge
    with tombstones kept, and iteration order. `store` must be empty; it is closed at the end."""
    store.append(event("task", "r1"))
    store.append(event("handover", "c1", "r1"))
    store.append(event("llm", "c1", "r1"))
    store.append(event("task", "r2"))
    store.append(event("schedule_run", None, None, schedule_id="s1"))
    store.append(event("done", "r1"))
    store.append(event("task_deleted", "r1"))
    assert store.count() == 7
    assert [d["kind"] for d in store.recent(2)] == ["done", "task_deleted"]
    assert [d["kind"] for d in store.seeds()] == ["task", "handover", "task", "done"]
    assert [d["kind"] for d in store.tree("r1")] == ["task", "handover", "llm", "done", "task_deleted"]
    assert [d["meta"]["schedule_id"] for d in store.by_kind_prefix("schedule_")] == ["s1"]
    assert store.by_kind_prefix("sched%") == []  # LIKE wildcards are escaped
    removed = store.purge("r1", {"r1", "c1"}, {"task_deleted"})
    assert removed == 4
    assert [d["kind"] for d in store.iter_all()] == ["task", "schedule_run", "task_deleted"]
    assert isinstance(store.describe(), dict) and store.describe()["backend"] == store.backend
    store.close()


def check_restart_keeps_tasks(tmp_path: Path, storage: dict, history_limit: int = 5):
    """Every task comes back after a restart, beyond the in-memory history cap. Returns the second bus."""
    import xqo.bus as busmod
    from xqo.config import build
    old = busmod.HISTORY_LIMIT
    busmod.HISTORY_LIMIT = history_limit
    try:
        cfg = Path(tmp_path) / "agents.json"
        cfg.write_text(json.dumps({"agents": [{"name": "planner", "skills": ["planning"]}], "storage": storage}))
        logs = str(Path(tmp_path) / "logs" / "events.jsonl")
        bus = build(cfg, Path(tmp_path) / "artifacts", logs, mock=True, departments="none")
        for i in range(history_limit + 1):
            bus.emit(busmod.Event("task", "user", "planner", f"t{i}", None, f"job {i}"))
            bus.emit(busmod.Event("done", "planner", "user", f"t{i}", None, "ok", {"status": "done"}))
        bus.store.close()
        again = build(cfg, Path(tmp_path) / "artifacts", logs, mock=True, departments="none")
        assert {f"t{i}" for i in range(history_limit + 1)} <= set(again.tasks)
        assert len(again.history) == history_limit and again.tasks["t0"].status == "done"
        return again
    finally:
        busmod.HISTORY_LIMIT = old
