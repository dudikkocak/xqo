# xqo

A small, plugin-based HQ of Claude agents: agents take tasks, hand work to each other by skill, run code in a
sandbox and ask you when they need a decision, all on a live dashboard with a 2D / 3D office view.

The `xqo` package is the **core** (agents, safety, dashboard), the **office** plugin and the **Claude Agent SDK**,
with events stored in **jsonl**. Everything else is a plugin package you install when you need it:

| Package | Adds |
|---|---|
| `xqo-schedules` | cron / every / at tasks |
| `xqo-feeds` | system, logs, files, journald, docker and push feeds that wake agents (needs schedules) |
| `xqo-channels` | Telegram, WhatsApp, Instagram, X, Threads, Gmail, SMS |
| `xqo-peering` | other HQs through an exchange folder or HTTP |
| `xqo-toolchains` | Node / Go / Rust / .NET setup for `run_code` |
| `xqo-docker` | the Docker sandbox backend for `run_code` |
| `xqo-agency` | `xqo agency`: import agents from agency-agents |
| `xqo-sqlite`, `xqo-postgres` | other event stores |

`pip install "xqo[all]"` installs every plugin. The plugin packages are being published one by one; until a
package is out, its feature is not available.

A plugin that is not installed is a **warning**, never an error (also with `"strict": true`): its config keys
(`schedules`, `channels`, `feeds`, `feed_rules`, `peering`) are ignored, the warning names the `pip install`
command, and the HQ starts. A `"storage"` backend or a `"sandbox"` backend that is not installed still stops
startup with the reason: events in another store are never silently shown as empty, and code never runs without
the isolation you asked for.

## Quick start

```bash
pip install xqo                    # core + office + Claude Agent SDK (mcp comes with it)
xqo init                           # writes a starter agents.json (+ departments/)
xqo agents.json                    # = xqo --config agents.json serve  ->  http://127.0.0.1:8000
xqo --mock --config agents.json serve   # offline, scripted model: no API key needed
```

A basic HQ (one office, 2D office only, no Docker, Claude Agent SDK by default, Anthropic / OpenAI APIs where an
agent asks for them) is in [examples/basic/agents.json](examples/basic/agents.json):

```bash
xqo examples/basic/agents.json check   # loads it, lists the plugins and the missing environment variables
xqo examples/basic/agents.json         # serve
```

Docker (Linux / macOS / WSL2: `./docker-startup.sh`, Windows: `docker-startup.bat`): [docs/DOCKER.md](docs/DOCKER.md),
Claude Agent SDK sign-in in Docker: [docs/CLAUDE-SDK.md](docs/CLAUDE-SDK.md).

## Core and plugins

| Core (always there) | Plugins (`xqo/plugins/`) |
|---|---|
| message bus, event log (jsonl), replay & fork | **office**: `/office`, 2D and/or 3D, editor *(on by default)* |
| agent loops: Claude Agent SDK (default), Anthropic API, OpenAI-compatible, Ollama | |
| workspaces, versions, git, attachments, notes | |
| chat, attention (requests + alerts, one inbox), reports | |
| departments, grants, skills, network grants, limits, budgets | |
| sandbox with the `local` backend (Python venvs), MCP hub | |
| dashboard, task lifecycle (archive + trash), limits, backup, drain, check | |

Turning plugins off:

```json
"plugins": {"disable": ["office"], "strict": false}
```

`/api/plugins` (and `/api/health`, `xqo check`, the `serve` banner) lists every plugin as **active**,
**inactive** (not configured), **disabled**, **missing** (not installed, or its module can't be imported; a
warning) or **failed** with the reason. A failed plugin's routes, tools and
dashboard buttons disappear; startup goes on. `"strict": true`, `XQO_STRICT=1` and `xqo check` turn plugin
errors back into startup errors. Security settings (grants, network grants, limits, departments) are core and
always fail closed.

Writing a plugin (its own pip package, importing only `xqo.api`), a storage or sandbox backend or dashboard UI:
see [PLUGINS.md](PLUGINS.md).

## Judged fit (optional)

Skill match stays the free first filter. With `"offer_judge": {"enabled": true, "model": "claude-haiku-5-5"}` a small model is asked only when two or more offers **tie** or the task is **large** (`large_chars`, default 2000), and it only affects who gets the handover. Prompt caching (shared prefix) and an in-memory fit cache (`cache_ttl`) keep the cost down; any failure falls back to the free ranking. Details: [docs/REFERENCE.md](docs/REFERENCE.md) (Departments → Judged fit).

## Storage

Events go to `logs/events.jsonl`, one line per event (`"storage": {"backend": "jsonl", "path": "events.jsonl"}`;
`path` is optional and relative to `logs/`). `xqo-sqlite` and `xqo-postgres` add `"backend": "sqlite"` and
`"backend": "postgres"`.

- Every task is restored at startup, whatever its age; only the recent 5000 events are kept in memory for the
  live views (older events load on demand, e.g. a task's full history or `/api/tasks?roots=true&limit=50&offset=50`).
- A backend that is not installed, or can't open, stops startup with the reason. `XQO_STORAGE` overrides
  `"backend"` for one run.

## Docs

- [docs/REFERENCE.md](docs/REFERENCE.md): every feature in detail (commands are `xqo …`)
- [docs/SETUP.md](docs/SETUP.md), [docs/OPERATIONS.md](docs/OPERATIONS.md): install, upgrade, backup
