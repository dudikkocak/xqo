# Run xqo in Docker

The image runs `xqo --config /data/agents.json serve` on port 8000. `./data` is mounted at `/data` (artifacts, logs,
your own `agents.json`); without `data/agents.json` the roster packaged in the image is used.

## Start

1. Install Docker (Linux: Docker Engine + Compose v2; Windows: Docker Desktop).
2. Set a sign-in: `ANTHROPIC_API_KEY`, or `CLAUDE_CODE_OAUTH_TOKEN` ([CLAUDE-SDK.md](CLAUDE-SDK.md)).
   A `.env` file next to `docker-compose.yml` works too.
3. From the xqo folder:

| | Linux / macOS / WSL2 | Windows |
|---|---|---|
| Build + start, wait until healthy | `./docker-startup.sh` | `docker-startup.bat` |
| Try offline, no key (mock LLM) | `./docker-startup.sh mock` | `docker-startup.bat mock` |
| Stop / logs / status | `./docker-startup.sh stop\|logs\|status` | `docker-startup.bat stop\|logs\|status` |
| Copy the Claude Code login into `.env` | `./docker-startup.sh token` | `docker-startup.bat token` |
| Check the Claude SDK sign-in | `./docker-startup.sh auth` | `docker-startup.bat auth` |
| Restart with new settings | `./docker-startup.sh restart --no-build` | `docker-startup.bat restart --no-build` |

Options:

| Option | What it does |
|---|---|
| `--node` | Node.js in the image (for `npx` MCP servers) |
| `--port N` | host port (default 8000, `XQO_HOST_PORT`) |
| `--no-build` | reuse the built image |

Dashboard: http://127.0.0.1:8000. It has **no login**, so it stays bound to 127.0.0.1.

Without the scripts: `docker compose up -d --build`.

## Notes

- The Claude Agent SDK is always in the xqo-lite image (`--sdk` is accepted and does nothing; `--toolchains` and
  `--docker-sandbox` are not included and stop the script). `--node` changes the image and needs a build, so leave out
  `--no-build` the first time you add one.
- Linux: `chmod +x docker-startup.sh` once if it is not executable.
- xqo runs as the app user (uid 1000). The entrypoint starts as root only to make `/data` writable for it (chown,
  else chmod). If neither works, as on some Docker Desktop mounts, it runs xqo as root inside the container and logs
  `xqo: /data is not writable for the app user …`. `XQO_RUN_AS=app` refuses that fallback, `XQO_RUN_AS=root` forces it.
- One server per data dir: don't run `mock` while `start` is running on the same `./data`.
- Upgrades and rollbacks by version tag: [OPERATIONS.md](OPERATIONS.md).
