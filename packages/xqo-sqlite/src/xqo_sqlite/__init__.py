"""xqo-sqlite: the "sqlite" event store for xqo.

    pip install xqo-sqlite
    "storage": {"backend": "sqlite"}                                  # logs/events.db
    "storage": {"backend": "sqlite", "path": "events.db", "busy_timeout_ms": 5000}

One SQLite file with indexed events: task pages, deletes and history reads don't scan the log. One server process
per data dir, like every xqo store. `xqo backup` writes a consistent copy through SQLite's online backup API.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Iterable, Iterator

from xqo.api import SEED_KINDS, EventStore, Plugin, register_store, store_path

__version__ = "3.1.0"
KEYS = {"backend", "path", "busy_timeout_ms"}


class SqliteStore(EventStore):
    """One SQLite file with indexed events: reads of one task tree, kinds and recent events don't scan the log.
    WAL mode (readers never block the writer), busy_timeout (a second writer waits instead of failing),
    synchronous=NORMAL (a crash loses at most the last commits, never the file), a quick_check when it opens."""
    backend = "sqlite"
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, kind TEXT, task_id TEXT, root_id TEXT, data TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS events_root ON events(root_id);
    CREATE INDEX IF NOT EXISTS events_task ON events(task_id);
    CREATE INDEX IF NOT EXISTS events_kind ON events(kind);
    """

    def __init__(self, path: str | Path, busy_timeout_ms: int = 5000):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        try:
            self.db = sqlite3.connect(str(self.path), timeout=busy_timeout_ms / 1000, check_same_thread=False,
                                      isolation_level=None)
            self.db.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            ok = self.db.execute("PRAGMA quick_check").fetchone()[0]
            if ok != "ok":
                raise ValueError(f"storage: {self.path} failed its integrity check ({ok}). Restore a backup, or "
                                 f"recover the readable rows: sqlite3 {self.path} \".recover\" | sqlite3 fixed.db")
            self.db.executescript(self.SCHEMA)
        except sqlite3.DatabaseError as e:
            raise ValueError(f"storage: can't open {self.path}: {e}") from None

    def _rows(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            rows = self.db.execute(sql, args).fetchall()
        return [json.loads(r[0]) for r in rows]

    def append(self, d: dict) -> None:
        with self._lock:
            self.db.execute("INSERT INTO events(ts, kind, task_id, root_id, data) VALUES (?, ?, ?, ?, ?)",
                            (d.get("ts"), d.get("kind"), d.get("task_id"), d.get("root_task_id"), json.dumps(d)))

    def recent(self, limit: int) -> list[dict]:
        return self._rows("SELECT data FROM (SELECT seq, data FROM events ORDER BY seq DESC LIMIT ?) ORDER BY seq",
                          (int(limit),))

    def seeds(self) -> list[dict]:
        marks = ",".join("?" * len(SEED_KINDS))
        return self._rows(f"SELECT data FROM events WHERE kind IN ({marks}) ORDER BY seq", SEED_KINDS)

    def tree(self, root_id: str) -> list[dict]:
        return self._rows("SELECT data FROM events WHERE root_id = ? OR task_id = ? ORDER BY seq", (root_id, root_id))

    def by_kind_prefix(self, prefix: str) -> list[dict]:
        esc = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return self._rows("SELECT data FROM events WHERE kind LIKE ? ESCAPE '\\' ORDER BY seq", (esc + "%",))

    def purge(self, root_id: str, ids: set[str], keep_kinds: Iterable[str]) -> int:
        keep = tuple(keep_kinds) or ("",)
        tids = tuple(ids) or ("",)
        with self._lock:
            cur = self.db.execute(
                f"DELETE FROM events WHERE kind NOT IN ({','.join('?' * len(keep))}) AND "
                f"(root_id = ? OR task_id = ? OR task_id IN ({','.join('?' * len(tids))}))",
                (*keep, root_id, root_id, *tids))
            return cur.rowcount

    def iter_all(self) -> Iterator[dict]:
        yield from self._rows("SELECT data FROM events ORDER BY seq")

    def count(self) -> int:
        with self._lock:
            return self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def backup_files(self, scratch: Path) -> dict[str, Path]:
        """A consistent copy through SQLite's online backup API (never a raw copy of a live database)."""
        scratch.mkdir(parents=True, exist_ok=True)
        dst = scratch / self.path.name
        dst.unlink(missing_ok=True)
        out = sqlite3.connect(str(dst))
        try:
            with self._lock:
                self.db.backup(out)
        finally:
            out.close()
        return {self.path.name: dst}

    def describe(self) -> dict:
        return {"backend": self.backend, "path": str(self.path), "events": self.count()}

    def close(self) -> None:
        with self._lock:
            self.db.close()



def _factory(cfg: dict, logs: Path) -> SqliteStore:
    unknown = set(cfg) - KEYS
    if unknown:
        raise ValueError(f"storage: unknown setting(s) {', '.join(sorted(unknown))} for sqlite; "
                         f"known: {', '.join(sorted(KEYS))}")
    return SqliteStore(store_path(cfg, logs, "events.db"), int(cfg.get("busy_timeout_ms", 5000)))


class SqlitePlugin(Plugin):
    name = "sqlite"
    description = "SQLite storage backend for the event log"

    @classmethod
    def register(cls) -> None:
        register_store("sqlite", _factory)
