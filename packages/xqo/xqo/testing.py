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


# ---- a live server for browser tests (playwright) ---------------------------------------------------------------
class LiveServer:
    """Serve an app on a free local port in a thread: `with LiveServer(create_app(bus)) as srv: page.goto(srv.url)`."""

    def __init__(self, app):
        import socket
        import threading

        import uvicorn
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        import time
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                break
            time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(5)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def launch_chromium(playwright):
    """A headless Chromium from playwright, or pytest.skip when none can start."""
    import glob

    import pytest
    for exe in (None, "/opt/pw-browsers/chromium", *sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome"))):
        try:
            return playwright.chromium.launch(executable_path=exe)
        except Exception:
            continue
    pytest.skip("no chromium available for playwright")
