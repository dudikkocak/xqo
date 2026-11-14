# xqo-lite

A trimmed build of xqo 1.2.0: the **core** (agents doing tasks, safely, with a live dashboard), the **office**
plugin (2D and 3D views, editor, idle routines) and the **Claude Agent SDK**, with events stored in **jsonl**.
The package is `xqo-lite`; the module and the command are still `xqo`, so configs and data dirs of full xqo work
as long as they don't depend on what is left out.

**Not included:** the schedules, channels, feeds, peering, toolchains, docker, agency and postgres plugins, and the
sqlite / postgres event stores. A missing plugin is a **warning**, never an error (also with `"strict": true`):
its config keys (`schedules`, `channels`, `feeds`, `feed_rules`, `peering`) are ignored and the HQ starts. A
`"storage"` backend other than `jsonl` (or `XQO_STORAGE=sqlite`) still stops startup with the reason, so existing
events in another store are never silently shown as empty. `"sandbox": {"backend": "docker"}` also stops startup
(it would otherwise run code without the isolation you asked for); use `auto` or `local`.

## Quick start

```bash
pip install -e .                   # core + office + Claude Agent SDK (mcp comes with it)
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
**inactive** (not configured), **disabled**, **missing** (not included, or its module can't be imported; a
warning) or **failed** with the reason. A failed plugin's routes, tools and
dashboard buttons disappear; startup goes on. `"strict": true`, `XQO_STRICT=1` and `xqo check` turn plugin
errors back into startup errors. Security settings (grants, network grants, limits, departments) are core and
always fail closed.

Writing a plugin, a sandbox backend or dashboard UI: see
[PLUGINS.md](PLUGINS.md).

## Judged fit (optional)

Skill match stays the free first filter. With `"offer_judge": {"enabled": true, "model": "claude-haiku-5-5"}` a small model is asked only when two or more offers **tie** or the task is **large** (`large_chars`, default 2000), and it only affects who gets the handover. Prompt caching (shared prefix) and an in-memory fit cache (`cache_ttl`) keep the cost down; any failure falls back to the free ranking. Details: [docs/REFERENCE.md](docs/REFERENCE.md) (Departments → Judged fit).

## Storage

Events go to `logs/events.jsonl`, one line per event (`"storage": {"backend": "jsonl", "path": "events.jsonl"}`
is the only form; `path` is optional and relative to `logs/`).

- Every task is restored at startup, whatever its age; only the recent 5000 events are kept in memory for the
  live views (older events load on demand, e.g. a task's full history or `/api/tasks?roots=true&limit=50&offset=50`).
- `sqlite` or `postgres` (from full xqo), or any other backend, stops startup with the reason.

## Docs

- [docs/REFERENCE.md](docs/REFERENCE.md): every feature in detail (commands are `xqo …`)
- [docs/SETUP.md](docs/SETUP.md), [docs/OPERATIONS.md](docs/OPERATIONS.md): install, upgrade, backup

## What changed from the original project

- **Renamed for 1.0.0, no aliases:** every `ORCH_*` environment variable is now `XQO_*` (`ORCH_DATA` → `XQO_DATA`...); the office config key for remote HQs is `hqs` (env var `XQO_OFFICE_HQS`) and its relay route `/api/office/hq/<name>/`; the peer header is `X-XQO-Peer`; backups are `xqo-backup-*`; caches live in `~/.cache/xqo/`; the Docker image is `xqo:<version>` with env var `XQO_VERSION` and compose service `xqo`.

- Package `orchestrator` → `xqo`, command `orchestrator` → `xqo`; the 1.0.0 renames below are not backward compatible.
- `scheduler.py`, `office.py`, `peers.py`, `agency.py`, `channels/`, `feeds/` and the Docker backend moved under
  `xqo/plugins/`. Office browser files are served from `/plugins/office/` (three.js and the office assets, 4 MB, only
  ship with the office plugin).
- One hook dispatcher (`xqo/plugin.py`) now gates tools for both agent loops (API and Claude Agent SDK). Before, the
  channels/feeds checks were written twice, once per loop.
- `config.py`, `web.py` and `__main__.py` no longer name optional features: plugins add their own config, routes,
  CLI commands, tools, prompt notes and listeners.
- New: `"office": {"views": ["2d"]}` (or `["3d"]`, `["2d", "3d"]`; first = default) and the `"plugins"` block.
- The default page title is "xqo" when agents.json has no `"name"`.
- Event storage is pluggable: jsonl, sqlite or postgres (see Storage), and all tasks come back after a restart (before,
  only the last 5000 events were restored).
- Alerts are part of attention (`attention.py`); archive and trash are one task lifecycle (`lifecycle.py`, one
  `artifacts/lifecycle.json` instead of `archive.json` + the trash index); every limit lives in `limits.py`
  (`bus.limits`: the sizes from `"limits"` and the no-limits hour).
- The Claude Agent SDK tool server is named `xqo` (tools are `mcp__xqo__<name>`, were `mcp__orchestrator__<name>`).
- Node / Go / Rust / .NET setup moved to the `toolchains` plugin; without it Python projects still get their venv.
- The dashboard is `static/index.html` + `static/app.css` + `static/js/*.js`; schedules, feeds and peering ship their
  own `ui.js`, loaded only while the plugin is active.
