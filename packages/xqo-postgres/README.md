# xqo-postgres

The `postgres` event store for [xqo](https://github.com/dudikkocak/xqo): for several machines or a hosted dashboard
that share one database. A single local HQ is better served by `xqo-sqlite`.

```bash
pip install xqo-postgres                   # brings psycopg 3
export XQO_PG_DSN="postgresql://xqo:secret@db.example:5432/xqo"
```

```json
"storage": {"backend": "postgres", "dsn_env": "XQO_PG_DSN", "table": "xqo_events"}
```

| Key | Default | |
|---|---|---|
| `dsn_env` | `XQO_PG_DSN` | the environment variable that holds the connection string (never put it in agents.json) |
| `table` | `xqo_events` | lowercase letters, digits and `_`; created with its indexes on first start |

Back it up with `pg_dump`: `xqo backup` leaves database contents to Postgres's own tools. The tests need a
database: `XQO_TEST_PG_DSN=postgresql://... pytest`.
