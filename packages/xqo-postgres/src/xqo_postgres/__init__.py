"""xqo-postgres: the "postgres" event store for xqo.

    pip install xqo-postgres                       # psycopg 3
    export XQO_PG_DSN="postgresql://xqo:secret@db.example:5432/xqo"
    "storage": {"backend": "postgres", "dsn_env": "XQO_PG_DSN", "table": "xqo_events"}

Use it when several machines or a hosted dashboard need one shared database; a single local HQ is better served by
xqo-sqlite. Back it up with pg_dump (xqo backup leaves database contents to Postgres's own tools).
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Iterable, Iterator

from xqo.api import SEED_KINDS, EventStore, Plugin, register_store

__version__ = "3.0.0"
KEYS = {"backend", "dsn_env", "table"}

TABLE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class PostgresStore(EventStore):
    backend = "postgres"

    def __init__(self, dsn: str, table: str = "xqo_events"):
        try:
            import psycopg
        except ImportError:
            raise ValueError("storage: the postgres backend needs psycopg: pip install xqo-postgres") from None
        if not TABLE.match(table):
            raise ValueError("storage.table: lowercase letters, digits and _ only")
        self.table = table
        self._lock = threading.Lock()
        try:
            self.db = psycopg.connect(dsn, autocommit=True, connect_timeout=10)
            self.db.execute(f"""
                CREATE TABLE IF NOT EXISTS {table} (
                    seq BIGSERIAL PRIMARY KEY, ts TEXT, kind TEXT, task_id TEXT, root_id TEXT, data JSONB NOT NULL);
                CREATE INDEX IF NOT EXISTS {table}_root ON {table}(root_id);
                CREATE INDEX IF NOT EXISTS {table}_task ON {table}(task_id);
                CREATE INDEX IF NOT EXISTS {table}_kind ON {table}(kind);""")
        except psycopg.Error as e:
            raise ValueError(f"storage: can't use Postgres: {str(e).strip().splitlines()[0]}") from None

    def _rows(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            rows = self.db.execute(sql, args).fetchall()
        return [r[0] if isinstance(r[0], dict) else json.loads(r[0]) for r in rows]

    def append(self, d: dict) -> None:
        with self._lock:
            self.db.execute(f"INSERT INTO {self.table}(ts, kind, task_id, root_id, data) VALUES (%s, %s, %s, %s, %s)",
                            (d.get("ts"), d.get("kind"), d.get("task_id"), d.get("root_task_id"), json.dumps(d)))

    def recent(self, limit: int) -> list[dict]:
        return self._rows(f"SELECT data FROM (SELECT seq, data FROM {self.table} ORDER BY seq DESC LIMIT %s) r "
                          "ORDER BY seq", (int(limit),))

    def seeds(self) -> list[dict]:
        return self._rows(f"SELECT data FROM {self.table} WHERE kind = ANY(%s) ORDER BY seq", (list(SEED_KINDS),))

    def tree(self, root_id: str) -> list[dict]:
        return self._rows(f"SELECT data FROM {self.table} WHERE root_id = %s OR task_id = %s ORDER BY seq",
                          (root_id, root_id))

    def by_kind_prefix(self, prefix: str) -> list[dict]:
        esc = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return self._rows(f"SELECT data FROM {self.table} WHERE kind LIKE %s ORDER BY seq", (esc + "%",))

    def purge(self, root_id: str, ids: set[str], keep_kinds: Iterable[str]) -> int:
        with self._lock:
            cur = self.db.execute(
                f"DELETE FROM {self.table} WHERE NOT (kind = ANY(%s)) AND "
                "(root_id = %s OR task_id = %s OR task_id = ANY(%s))",
                (list(keep_kinds), root_id, root_id, list(ids)))
            return cur.rowcount

    def iter_all(self) -> Iterator[dict]:
        yield from self._rows(f"SELECT data FROM {self.table} ORDER BY seq")

    def count(self) -> int:
        with self._lock:
            return self.db.execute(f"SELECT COUNT(*) FROM {self.table}").fetchone()[0]

    def describe(self) -> dict:
        return {"backend": self.backend, "table": self.table, "events": self.count()}

    def close(self) -> None:
        with self._lock:
            self.db.close()


def _factory(cfg: dict, logs: Path) -> PostgresStore:
    unknown = set(cfg) - KEYS
    if unknown:
        raise ValueError(f"storage: unknown setting(s) {', '.join(sorted(unknown))} for postgres; "
                         f"known: {', '.join(sorted(KEYS))}")
    env = cfg.get("dsn_env") or "XQO_PG_DSN"
    dsn = os.getenv(env)
    if not dsn:
        raise ValueError(f"storage: set the environment variable {env} to the Postgres DSN (storage.dsn_env)")
    return PostgresStore(dsn, str(cfg.get("table") or "xqo_events"))


class PostgresPlugin(Plugin):
    name = "postgres"
    description = "Postgres storage backend for the event log"

    @classmethod
    def register(cls) -> None:
        register_store("postgres", _factory)
