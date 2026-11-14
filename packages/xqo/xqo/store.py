"""Where xqo keeps its event log, the source of truth for tasks, replay, forks and the dashboard history.

    "storage": {"backend": "jsonl"}                                   # the only backend: logs/events.jsonl
    "storage": {"backend": "jsonl", "path": "events.jsonl"}           # optional file name (relative to logs/)

xqo-lite stores events in a JSONL file only. "sqlite" and "postgres" (in full xqo) are not included, and asking for
them stops startup with the reason, so a data dir written by another backend is never silently shown as empty.
XQO_STORAGE overrides "backend". Relative paths are relative to the data dir's logs/ folder.

Storage is core: if the backend can't open, startup stops with the reason.
Workspaces, attachments and runtime state files (usage.json, ...) stay files.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Iterable, Iterator

# the event kinds task records are rebuilt from (bus._ingest); loaded in full at startup, whatever the history cap
SEED_KINDS = ("task", "direct", "handover", "done", "task_paused", "task_resumed", "task_reopened", "user_message")


def _matches_tree(d: dict, root_id: str, ids: set[str] | None = None) -> bool:
    return d.get("root_task_id") == root_id or d.get("task_id") == root_id or (ids is not None and d.get("task_id") in ids)


class EventStore:
    """The interface every backend implements. Events are dicts (Event.to_dict() plus routing fields)."""
    backend = ""

    def append(self, d: dict) -> None:
        raise NotImplementedError

    def recent(self, limit: int) -> list[dict]:
        """The newest `limit` events, oldest first."""
        raise NotImplementedError

    def seeds(self) -> list[dict]:
        """Every event of a SEED_KINDS kind, oldest first (rebuilds all task records, not only recent ones)."""
        return [d for d in self.iter_all() if d.get("kind") in SEED_KINDS]

    def tree(self, root_id: str) -> list[dict]:
        """Every event of one task tree, oldest first."""
        return [d for d in self.iter_all() if _matches_tree(d, root_id)]

    def by_kind_prefix(self, prefix: str) -> list[dict]:
        return [d for d in self.iter_all() if str(d.get("kind", "")).startswith(prefix)]

    def purge(self, root_id: str, ids: set[str], keep_kinds: Iterable[str]) -> int:
        """Remove a task tree's events except `keep_kinds` (tombstones). Returns how many were removed."""
        raise NotImplementedError

    def iter_all(self) -> Iterator[dict]:
        raise NotImplementedError

    def count(self) -> int:
        return sum(1 for _ in self.iter_all())

    def backup_files(self, scratch: Path) -> dict[str, Path]:
        """Files that hold the log, as {name in the archive's data/logs/: path}; written consistently to `scratch`
        when the live file can't be copied as it is. {} when the data is not in a file (Postgres)."""
        return {}

    def describe(self) -> dict:
        return {"backend": self.backend}

    def close(self) -> None:
        pass


class JsonlStore(EventStore):
    """One JSON object per line, appended. Simple and greppable; reads scan the file."""
    backend = "jsonl"

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(self, d: dict) -> None:
        line = json.dumps(d) + "\n"
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(line)

    def iter_all(self) -> Iterator[dict]:
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    def recent(self, limit: int) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines()[-limit:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def seeds(self) -> list[dict]:
        keys = tuple(f'"kind": "{k}"' for k in SEED_KINDS)
        out = []
        if not self.path.exists():
            return out
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                if not any(k in line for k in keys):  # cheap pre-filter before parsing
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("kind") in SEED_KINDS:
                    out.append(d)
        return out

    def tree(self, root_id: str) -> list[dict]:
        out = []
        if not self.path.exists():
            return out
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                if root_id not in line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if _matches_tree(d, root_id):
                    out.append(d)
        return out

    def purge(self, root_id: str, ids: set[str], keep_kinds: Iterable[str]) -> int:
        keep_kinds = set(keep_kinds)
        if not self.path.exists():
            return 0
        keep, removed = [], 0
        with self._lock:
            with self.path.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        keep.append(line)
                        continue
                    if d.get("kind") not in keep_kinds and _matches_tree(d, root_id, ids):
                        removed += 1
                    else:
                        keep.append(line)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text("".join(keep), encoding="utf-8")
            os.replace(tmp, self.path)
        return removed

    def count(self) -> int:
        if not self.path.exists():
            return 0
        with self.path.open("rb") as f:
            return sum(1 for _ in f)

    def backup_files(self, scratch: Path) -> dict[str, Path]:
        return {self.path.name: self.path} if self.path.is_file() else {}

    def describe(self) -> dict:
        return {"backend": self.backend, "path": str(self.path)}


# ---- choosing a backend ------------------------------------------------------------------------------------
KEYS = {"backend", "path"}
NOT_INCLUDED = {"sqlite", "postgres"}  # backends of full xqo that xqo-lite leaves out


def _path(cfg: dict, logs: Path, default: str) -> Path:
    p = Path(str(cfg.get("path") or default)).expanduser()
    return p if p.is_absolute() else logs / p


def open_store(cfg: dict | None, log_path: str | Path) -> EventStore:
    """The store "storage" in agents.json asks for; `log_path` (.../logs/events.jsonl) sets the logs dir and the
    default file."""
    cfg = dict(cfg or {})
    unknown = set(cfg) - KEYS
    if unknown:
        raise ValueError(f"storage: unknown setting(s) {', '.join(sorted(unknown))}; known: {', '.join(sorted(KEYS))}")
    backend = os.getenv("XQO_STORAGE") or cfg.get("backend") or "jsonl"
    log_path = Path(log_path)
    if backend != "jsonl":
        why = " is not included in xqo-lite" if backend in NOT_INCLUDED else " is unknown"
        raise ValueError(f"storage: backend {backend!r}{why}; xqo-lite stores events in jsonl only "
                         f"(remove \"storage\" from agents.json and unset XQO_STORAGE)")
    if "path" not in cfg:
        return JsonlStore(log_path)
    return JsonlStore(_path(cfg, log_path.parent, "events.jsonl"))
