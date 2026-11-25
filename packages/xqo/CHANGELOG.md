# Changelog

## Unreleased

- **Optional login** (off by default, [docs/AUTH.md](docs/AUTH.md)): Google sign-in (OpenID Connect with PKCE) and
  Telegram (a bot link you approve in Telegram, no public domain needed), an allowlist of emails, `*@domain` and
  `tg:<id>`, and an API token (`XQO_API_TOKEN`) for scripts and the `xqo` command, which sends it by itself. A signed-in
  person can link their Google and Telegram accounts so either signs in as them. The middleware guards every page,
  API route and the event stream; channel webhooks and feed pushes keep their own secrets; `/api/health` answers
  `{"ok": true}` without a sign-in. Cookie requests that change something must come from the same site.
- `office.hqs` entries take `token_env`, sent to that HQ as a bearer token by the office relay.
- `xqo serve` warns when it listens beyond localhost without login (not inside Docker, where the port mapping decides).

## 3.1.1

- Releases upload to PyPI with Trusted Publishing (OIDC) instead of a stored API token.
- CI uses `actions/checkout@v5` and `astral-sh/setup-uv@v7` (Node.js 24).

## 3.1.0

- New plugin packages **`xqo-toolchains`** (Node, Go, Rust and .NET for `run_code`) and **`xqo-agency`** (the
  `xqo agency` importer, whose files `xqo backup` keeps through `Plugin.config_files`).
- New plugin packages **`xqo-schedules`** (cron / every / at tasks, the ⏱ panel and `xqo schedule`) and
  **`xqo-feeds`** (local feeds and rules, `xqo feed`; it depends on `xqo-schedules`). Without schedules, feeds
  shows as `missing`. Feeds find the scheduler through `ctx.get("schedules")`; `bus.scheduler` and `bus.feeds`
  are still set while the plugins are active. Backups skip `scheduler.lock` and keep `artifacts/feeds/`.
- New plugin packages **`xqo-channels`** (messaging channels, `xqo channels`) and **`xqo-peering`** (other HQs,
  `xqo peers`, the ⇆ peers panel). Backups keep `artifacts/channels/` and `artifacts/.peers/`. The office reads
  its HQ name from peering through `bus.plugins.service("peering")`. `xqo.api` adds `max_file_bytes()`.
- With this release every plugin of xqo 1.2.0 is a package again.
- Checked against the xqo 1.2.0 test suite: with `xqo[all]` installed, 590 of its tests pass. The other 7 test
  behaviour that 3.x changed on purpose:
  - `bus.scheduler`, `bus.feeds`, `bus.channels` and `bus.peers` are not set at all while their plugin is inactive
    (1.2.0 set them to `None`). Use `bus.plugins.service("<name>")`, which returns `None`.
  - `/api/health` has no `scheduler` key and `/api/config` has no `peering` key; plugin state is in `/api/plugins`.
  - An unknown storage backend is reported as "backend '<name>' is not installed; available: ...".
- New extras `xqo[toolchains]`, `xqo[agency]`, `xqo[schedules]`, `xqo[feeds]`, `xqo[automation]` (schedules and
  feeds), `xqo[channels]` and `xqo[peering]`; `xqo[all]` includes them.

## 3.0.0 (2026-11-14)

First release of xqo as a base package plus plugin packages, from one monorepo. The `xqo` package holds the core,
the office plugin (2D and 3D) and the Claude Agent SDK, and stores events in jsonl.

- **Plugins are pip packages.** They are found through the `xqo.plugins` entry point group. Known plugins that
  are not installed (`xqo-schedules`, `xqo-channels`, `xqo-feeds`, `xqo-peering`, `xqo-toolchains`, `xqo-docker`,
  `xqo-agency`, `xqo-sqlite`, `xqo-postgres`) show as `missing` in `/api/plugins`; a config that uses their keys
  gives a warning with the `pip install` command, never an error (also in strict mode).
- **Public plugin API `xqo.api`** (API version 1): the plugin base class and context, the bus and its records,
  storage and sandbox backend registration, the toolchains hook, attachments, departments, branding, avatars,
  `build` and the CLI client. Everything else in the package is private.
- **Storage backends are pluggable** again (`register_store`); the base package registers `jsonl`. A backend
  that is not installed stops startup and names its package.
- **Backups ask the plugins** which folders under `artifacts/` hold runtime settings (`Plugin.state_dirs`),
  which files to leave out (`Plugin.skip_files`) and which config files to include (`Plugin.config_files`).
- The core no longer names optional features: no fixed `bus.scheduler` / `bus.peers` / `bus.channels` /
  `bus.feeds` slots, no `scheduler` key in `/api/health`, no `peering` key in `/api/config` (plugin status is
  in `/api/plugins`).
- **First plugin packages:** `xqo-sqlite` and `xqo-postgres` (event stores) and `xqo-docker` (sandbox backend),
  each tested against the base package alone. `xqo.api` adds what a container backend needs (`SandboxConfig`,
  `current_toolchains`, `docker_kill`, `docker_user`, `net_args`, `setup_net_args`, `DOCKER_PATH`,
  `is_python_project`, `mount_hint`, `uv_pip_command`), and `xqo.testing` holds the storage contract checks.
- Docker: image `xqo:<version>`; build arg `PLUGINS` (compose: `XQO_PLUGINS`) installs plugin packages.
