# xqo-sqlite

The `sqlite` event store for [xqo](https://github.com/dudikkocak/xqo): one SQLite file with indexed events, so task
pages, deletes and history reads don't scan the log.

```bash
pip install xqo-sqlite
```

```json
"storage": {"backend": "sqlite"}
```

| Key | Default | |
|---|---|---|
| `path` | `events.db` | relative to the data dir's `logs/` folder |
| `busy_timeout_ms` | `5000` | how long a second writer waits instead of failing |

WAL mode (readers never block the writer), `synchronous=NORMAL` (a crash loses at most the last commits, never the
file), and a quick integrity check when it opens. `xqo backup` writes a consistent copy with SQLite's online
backup API. `XQO_STORAGE=sqlite` picks it for one run. Switching backends starts an empty store: events are not
copied between stores.
