"""xqo-postgres: the storage contract and restarts against a real database (XQO_TEST_PG_DSN), plus config checks."""
import json
import os
import uuid

import pytest

from xqo.config import build
from xqo.store import open_store
from xqo.testing import check_event_store, check_restart_keeps_tasks
from xqo_postgres import PostgresPlugin, PostgresStore

PG_DSN = os.getenv("XQO_TEST_PG_DSN")  # e.g. postgresql://xqo@/xqo_test?host=/tmp&port=55432
needs_pg = pytest.mark.skipif(not PG_DSN, reason="set XQO_TEST_PG_DSN to test against Postgres")


@needs_pg
def test_contract():
    check_event_store(PostgresStore(PG_DSN, "t_" + uuid.uuid4().hex[:12]))


@needs_pg
def test_all_tasks_survive_a_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("XQO_PG_DSN", PG_DSN)
    table = "b_" + uuid.uuid4().hex[:12]
    again = check_restart_keeps_tasks(tmp_path, {"backend": "postgres", "table": table})
    assert again.store.describe()["table"] == table
    assert not (tmp_path / "logs" / "events.jsonl").exists()


def test_found_as_a_plugin_and_needs_its_dsn(tmp_path, monkeypatch):
    monkeypatch.delenv("XQO_PG_DSN", raising=False)
    cfg = tmp_path / "agents.json"
    cfg.write_text(json.dumps({"agents": [{"name": "planner", "skills": ["planning"]}],
                               "storage": {"backend": "postgres"}}))
    with pytest.raises(ValueError, match="XQO_PG_DSN"):
        build(cfg, tmp_path / "a", str(tmp_path / "l" / "e.jsonl"), mock=True, departments="none")


def test_bad_config_stops_startup(tmp_path):
    PostgresPlugin.register()
    with pytest.raises(ValueError, match="dsn_env"):
        open_store({"backend": "postgres", "dsn": "postgresql://x"}, tmp_path / "events.jsonl")
    with pytest.raises(ValueError, match="unknown setting.*for postgres"):
        open_store({"backend": "postgres", "path": "x"}, tmp_path / "events.jsonl")
    with pytest.raises(ValueError, match="lowercase"):
        PostgresStore("postgresql://unused", "Bad-Name")
