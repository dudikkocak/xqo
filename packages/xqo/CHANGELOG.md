# Changelog

Changes since xqo 1.1.2. Newest first.

## xqo-lite 1.2.0 (2026-11-13)

A trimmed build of xqo 1.2.0: core + office (2D and 3D) + Claude Agent SDK, events in jsonl only.

### Removed
- Plugins `schedules`, `channels`, `feeds`, `peering`, `toolchains`, `docker`, `agency`, `postgres` (their folders,
  tests, CLI commands and dashboard UI), `deploy/multi/`, `docker-compose.sandbox.yml`, `docs/CHANNELS.md`,
  `docs/FEEDS.md`, `docs/TOOLCHAINS.md`.
- The `sqlite` event store (`SqliteStore`) and `register_store`: `xqo/store.py` has only the jsonl store. Any
  other `"storage"` backend or `XQO_STORAGE` value stops startup with "not included in xqo-lite".
- Optional extras `mcp`, `sdk`, `feeds`, `postgres`. Docker: the toolchain and docker-CLI build stages, build args
  `EXTRAS`, `WITH_TOOLCHAINS`, `GO_IMAGE`, `RUST_IMAGE`, `DOTNET_IMAGE`, `WITH_DOCKER_CLI`; the startup scripts'
  `--toolchains` and `--docker-sandbox` (now an error that says so).

### Changed
- Package name `xqo-lite` (module and command stay `xqo`); `claude-agent-sdk` is a regular dependency (it brings
  `mcp`). Docker image `xqo-lite:<version>`; `--sdk` in the startup scripts is accepted and does nothing.
- **Missing plugins are warnings, not errors.** New plugin state `missing` (in `/api/plugins`, `/api/health`,
  `xqo check`) for a plugin that is not included (`xqo.plugins.NOT_INCLUDED`), whose module can't be imported, or
  whose requirement is missing. A config that still uses a left-out plugin's keys (`schedules`, `channels`,
  `feeds`, `feed_rules`, `peering`) gives one warning per plugin and those keys are ignored. This holds with
  `"strict": true`, `XQO_STRICT=1` and `xqo check` too; other plugin errors still stop startup in strict mode.
  Naming a left-out plugin in `plugins.disable` is fine (no warning).
- `"sandbox": {"backend": "docker"}` still stops startup (it would otherwise run code without the asked-for
  isolation); the message now says the docker backend is not included.

### Tests
- 420 passed, 47 skipped (browser and Docker tests skip here). New tests for missing-plugin warnings (also in
  strict mode), import failures, missing requirements and the jsonl-only store.

## 1.2.0 (2026-11-13)

### Docs
- Removed the remaining references to the predecessor project name from `README.md` and `docs/REFERENCE.md`
  (intro, docs list, the "What changed" heading, the rename notes and the event-storage note). Wording only.
- Version bumped to 1.2.0 (`xqo/__init__.py`, default image tag in `docker-compose.yml` and
  `deploy/multi/docker-compose.yml`). No functional changes.

## 1.1.6 (2026-11-12)

### Docker
- **Startup scripts.** `docker-startup.sh` (Linux / macOS / WSL2) and `docker-startup.bat` (Windows) build and run
  xqo, wait until `/api/health` answers, and offer `start`, `stop`, `restart`, `logs`, `status` and `mock` (offline,
  no key). The sign-in warning also looks in `.env`. Options: `--sdk`, `--node`, `--toolchains`, `--docker-sandbox` (Linux only), `--port N`, `--no-build`.
- **`--toolchains`** / build arg `WITH_TOOLCHAINS=1`: Go (`GO_IMAGE`, default 1.23), Rust (`RUST_IMAGE`, stable)
  and .NET (`DOTNET_IMAGE`, default `sdk:latest`, the newest release) copied from their official images into `/opt/tc`, plus the C toolchain and ICU; Node
  through `WITH_NODE=1`. Off by default; nothing extra is pulled then.
- **`--docker-sandbox`**: new `docker-compose.sandbox.yml` runs `run_code` on the host's Docker (socket mount,
  `./data` at its host path, `XQO_SANDBOX=docker`). New build arg `WITH_DOCKER_CLI=1` adds the docker CLI.
- `docker-compose.yml`: host port `XQO_HOST_PORT` (default 8000), passes `CLAUDE_CODE_OAUTH_TOKEN` (claude.ai
  subscription sign-in for `--sdk`), build args `WITH_TOOLCHAINS`, `GO_IMAGE`, `RUST_IMAGE`, `DOTNET_IMAGE` (settable in `.env`).
- The app user is created with uid 1000 explicitly.
- **Fixed: `PermissionError: '/data/artifacts'` at start** when the mounted `./data` was not writable for uid 1000
  (Docker Desktop on Windows, or a folder owned by another user on Linux). New `docker-entrypoint.sh` starts as root,
  makes the data dir writable (chown, else chmod), then runs xqo as the app user with `setpriv`, keeping groups from
  `group_add`. If the folder still can't be written, xqo runs as root inside the container and says so
  (`XQO_RUN_AS=app|root|auto`). `.dockerignore` now leaves out `data/` and `.env`.

### Claude Agent SDK
- **Fixed: a rejected `CLAUDE_CODE_OAUTH_TOKEN` was reported as "No Anthropic credentials: ANTHROPIC_API_KEY is not
  set".** When the SDK was only the default provider and Claude Code could not start or sign in, xqo fell back to the
  Anthropic API even without a key, which hid Claude Code's own error. It now falls back only when `ANTHROPIC_API_KEY`
  is set; otherwise the task fails with the sign-in error, which also names `CLAUDE_CODE_OAUTH_TOKEN`.

- **Sign-in check:** `python -m xqo.sdk_check`, or `docker-startup.sh auth` / `docker-startup.bat auth` against the
  running container. Shows the credentials in the environment (masked) with common paste mistakes, the sign-in xqo
  will use, and the result of one tiny call with the bundled Claude Code. Exit code 0 when it answers.

- **A renewed OAuth token applies to the next chat, without a restart.** Before every Claude Code session xqo reads
  `CLAUDE_CODE_OAUTH_TOKEN` from `XQO_OAUTH_TOKEN_FILE` (a bare token or a .env file), else
  `<data dir>/secrets/claude-oauth-token`; it wins over the environment variable. `docker-compose.yml` mounts the xqo
  folder read-only at `/run/xqo/config` and points the variable at its `.env`, so editing `.env` is enough. The
  dashboard, `xqo.sdk_check` and the sign-in error name the file.

- **`docker-startup token`** (`claude-token-to-env.sh` / `.ps1`): copies the Claude Code login token from
  `~/.claude/.credentials.json` into `.env` as `CLAUDE_CODE_OAUTH_TOKEN`, on demand. It is the short-lived access
  token, so an expired one is refused unless forced.

### Docs
- New `docs/DOCKER.md` (scripts and options), `docs/CLAUDE-SDK.md` (SDK sign-in in Docker: API key or claude.ai
  OAuth token, renewing an expired token, mounting a Claude Code login), `docs/TOOLCHAINS.md` (Go, Rust, .NET, Node:
  in the image or on the Docker sandbox backend).

## 1.1.5 (2026-11-05)

### Office
- **Click a monitor to call an agent back.** In 2D and 3D, clicking an agent's monitor, desk or chair opens its side panel. A desk without a computer works too. If the agent is away from its desk (idle, at a routine, or resting while paused), it says "On my way", hurries back and stays seated while the panel is open, for at most 60 seconds (`office.recallMs`). Closing the panel or selecting another agent lets it go. Working agents, and agents waiting for you, only get the panel. Nothing is sent to the agent: this changes only what the office shows.
- In 2D, a called-back agent sits in the desk (typing) pose, and its screen stays dark.
- New `desksOf()` / `deskAgent()` in `officelayout.js` find each agent's desk: the prop with its seat, or its own automatic desk. A desk with several seats picks the seat nearest the click.

### Tests
- `tests/test_office_recall.py`: desk lookup (node), and monitor and plain-desk clicks in 2D and 3D, recall expiry, working agents.

## 1.1.4 (2026-11-05)

### Artifacts
- **source ⬇** download on each task group: a zip of the workspace's project files only (`GET /api/artifacts/zip/<task>?source_only=true`).
  - Always left out: dependency, build and cache folders (`node_modules`, `.venv`/`venv` or any folder with `pyvenv.cfg`, `__pycache__`, `dist`, `build`, `.next`, `.gradle`, tool caches, `.git`) and `*.pyc`.
  - Left out only next to the project file that owns them: `target` (Cargo/Maven), `vendor` (Go, PHP, Ruby) and `bin`/`obj` (.NET). A plain `bin/` or `vendor/` folder is kept.
- **workspace ⬇** and **all ⬇** are unchanged.

### Docs
- REFERENCE.md (zip endpoint) and SETUP.md (downloads) mention **source ⬇**.

## 1.1.3 (2026-11-05)

### Dashboard
- **Artifacts grouped by task.** The Artifacts panel has one collapsible group per task, with department workspaces (`<task>.<dept>`) nested under their task. Each group shows the task id, its title, the file count and the tools. Open and closed state is remembered per browser. New **active task only** switch, and **collapse all** / **expand all**.
- **Resume responds at once.** The button shows "⏳ resuming…" and the user node pulses ("resumed · waiting…") until an agent responds, as New task, Ask and Steer already did.

### Office
- **Resume in the side panel whenever it is possible.** It resumes a paused task and also reopens a finished one in the same workspace. It follows the task's root. The agent says "▶ Resuming…" and hurries to its desk before the server answers. If the call fails, the button is restored.
- **The 3D camera is restored on the next visit.** It is saved per office (this HQ, another HQ, the planners room) every second and when you leave the page. A rebuild keeps the current camera.
- **New default 3D view:** from the front-left corner, slightly above, zoomed so the whole office fits.

### Tests
- `tests/test_artifacts_groups.py`, `tests/test_office_resume_camera.py`.

## Every release
- Version bumped in `xqo/__init__.py`, both `docker-compose.yml` files (`XQO_VERSION` default). The roll-back example in `docs/OPERATIONS.md` names 1.1.4.
