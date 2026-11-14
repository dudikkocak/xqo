> **xqo-lite.** This build has only the core, the office plugin and the Claude Agent SDK, and stores events in
> jsonl. Parts about schedules, channels, feeds, peering, toolchains, the Docker sandbox, agency, sqlite or
> postgres don't apply here (see ../README.md). Missing plugins are startup warnings, not errors.

# Operations: skills, tools and MCP per agent · backups · upgrades

## 1. Who gets what: one rule for MCP servers, skill packs and tools

Every grant is looked up like `model`: **the agent's own field → its department's `default_*` → `default_*` in agents.json → nothing.**

| What | Agent field | Department / agents.json default |
|---|---|---|
| MCP servers | `mcp` | `default_mcp` |
| Skill packs | `skill_packs` | `default_skill_packs` |
| Tool policy | `tools` | `default_tools` |

- A list **replaces** what it inherits. Entries written `"+name"` are **added** to it (a list of only `+` entries keeps the inherited ones). `[]` removes everything. `"all"` = everything the agent can see.
- For **all agents**: put it in agents.json. For **one department**: in the department file. For **one agent**: in the agent.

```json
// agents.json
"mcp_servers": [{"name": "docs", "url": "https://docs.example.com/mcp", "token_env": "DOCS_TOKEN"}],
"default_mcp": ["docs"],                       // every agent
"default_skill_packs": ["brand-voice"],
"default_tools": {"deny": ["mcp__*__delete_*"]},

// departments/engineering.json
"mcp_servers": [{"name": "github", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
                 "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "..."}}],
"default_mcp": ["+github"],                    // engineering: docs + github
"skills_dir": "engineering-skills",            // packs only engineering can be granted
"default_skill_packs": "all",

// one agent
{"name": "reviewer", "mcp": {"github": ["get_*", "search_*"], "+docs": "*"},   // read-only GitHub tools + docs
 "skill_packs": ["+code-review"], "tools": {"deny": ["+run_code"]}}
```

### MCP servers (new tools)
- New tools are added as MCP servers, so xqo's code doesn't change. A local server is a small program (for example Python with `FastMCP`, see `tests/mcp_echo_server.py`) under `mcp_servers` with `command`/`args`. A remote one has a `url`.
- `mcp` as an object filters the tools per agent: `{"server": ["pattern", ...]}` (fnmatch on the server's tool names) or `"*"`. Remote servers need exact names, because their tools aren't listed in advance. A server's own `allowed_tools` still applies to everyone.
- A server that an agent isn't granted is refused even if the model names its tool.
- **Hot reload:** `POST /api/departments/reload` (the dashboard's reload) re-reads agents.json, the department files and the skill packs. Local servers are started, restarted (config changed) or stopped to match, and the result has `"mcp": {"added", "removed", "restarted"}`.
- Docker: build with `WITH_NODE=1` for `npx` servers (`docker compose build --build-arg WITH_NODE=1`), or run them elsewhere as remote servers.

### Skill packs
```
skills/<name>/SKILL.md          ---\nname: <name>\ndescription: when to use it\n---\n instructions
skills/<name>/reference/*.md    optional files the instructions point to
skills/<name>/scripts/*         optional scripts
```
- Where packs come from: `skills/` next to agents.json (or `"skills_dir"`, or `XQO_SKILLS_DIR`) for everyone, plus a department file's `"skills_dir"` (relative to the file) for its members only. Names must be unique.
- An agent's prompt lists only the name and description of each granted pack. `load_skill(name)` returns the instructions and the file list, and `read_skill_file(name, path)` returns one file. `run_code` (and Bash for claude-sdk agents) finds the packs at `.skills/<name>/` in its run copy. Nothing is written to the workspace.
- This works with every provider (Anthropic, OpenAI-compatible, claude-sdk).

### Tool policy
`{"allow": [...], "deny": [...]}`, with fnmatch patterns over the final tool names (`handover_task`, `note`, `run_code`, `mcp__github__create_issue`, Claude Code's `Bash`, `Write`, ...). A tool is offered when it matches `allow` (no `allow` = every tool) and no `deny` pattern. Each list inherits with the same `+` rule. For remote MCP servers, `mcp__<server>__*` drops the whole server and an exact `mcp__<server>__<tool>` turns that one tool off. Channel and feed task limits still apply on top.

The dashboard's agent data (`/api/agents`) shows the result for each agent: `mcp`, `mcp_filters`, `mcp_source`, `skill_packs`, `skill_packs_source`, `tool_policy`. Names an agent can't see (another department's server or pack) are skipped with a warning at start-up and in `xqo check`.

## 2. Backups

```bash
xqo backup                      # full: config + all data (through the running server)
xqo backup --scope state        # config + runtime settings: schedules, office, usage, channel/feed/peering state
xqo backup --scope config       # agents.json, departments/, agency files, skills/, avatars
xqo backup --keep 14            # and keep the newest 14 of that scope
xqo backup list | verify ARCHIVE | restore ARCHIVE --to NEW_FOLDER [--dry-run]
```
- With a running server (`--url` / `XQO_URL`), the server writes the archive while the scheduler, feeds, channel polling and the peering exchange-folder polling pause for a few seconds. Running tasks keep going and are listed in the manifest. Without a server it's written directly (`--offline` forces that).
- Archives go to `"backup": {"dir": "...", "keep": N}` in agents.json, else `<data>/backups/` (that folder is never backed up itself). Name: `xqo-backup-<UTC>-<scope>.tar.gz` with `MANIFEST.json` (version, data version, sha256 per file).
- **Never in an archive:** channel sign-in tokens (`artifacts/channels/*.tokens.json`: sign in again after a restore), secrets (they live in environment variables), virtual environments, run copies, `*.tmp`, `scheduler.lock`.
- **Secrets:** keep the environment (`ANTHROPIC_API_KEY`, `*_env` variables) in a `.env` file or a secret store outside the data folder, and back it up separately (password manager). `xqo check` lists every `*_env` variable that isn't set.
- `restore` checks every file, refuses to write into a folder that has files, and puts config and data in one folder (the Docker `/data` layout). With a master config, keep `"server": {"data_dir": "."}` in the restored agents.json (restore prints a note otherwise). Then run `xqo --config NEW/agents.json --data-dir NEW check` and start it.
- **Event store:** jsonl copies `events.jsonl`; sqlite writes a consistent copy of `events.db` with SQLite's online backup (safe while the server writes); postgres is not in the archive: back the table up with `pg_dump -t xqo_events` (see Storage in the README).
- Nightly (host cron): `0 3 * * * cd /srv/xqo && xqo backup --keep 14 && xqo feed emit backup --status 0 || xqo feed emit backup --status 1`, then copy `backups/` off the machine (restic, rclone, ...).

## 3. Upgrading a running instance

Releases are versions (`xqo --version`, `/api/health`), built as Docker images tagged with the version. The data dir carries `artifacts/DATA_VERSION`.

| Step | Docker | Native (venv) |
|---|---|---|
| 1. Back up | `docker compose exec xqo xqo backup` | `xqo backup` |
| 2. Get the new version | unpack the new zip; `XQO_VERSION=1.2.0 docker compose build` | unpack into `/opt/xqo/releases/1.2.0`, `uv venv && uv pip install ".[mcp,sdk]"` |
| 3. Check it with the new code | `XQO_VERSION=1.2.0 docker compose run --rm xqo xqo check --mcp` | `/opt/xqo/releases/1.2.0/.venv/bin/xqo --config ... --data-dir ... check --mcp` |
| 4. Drain | `docker compose exec xqo xqo drain` | `xqo drain` |
| 5. Switch | `XQO_VERSION=1.2.0 docker compose up -d` | `ln -sfn releases/1.2.0 /opt/xqo/current && systemctl restart xqo` |
| 6. Verify | `curl -s localhost:8000/api/health` | same |
| Roll back | `XQO_VERSION=1.1.4 docker compose up -d` | point `current` back, restart |

- `check` loads the configuration with the new code without starting anything or touching the data. It reports errors, warnings, unset `*_env` variables, MCP servers that don't start (`--mcp`), and the migrations the new version will run.
- `drain` stops new tasks (API answers 503), schedules (they catch up after the restart), feed tasks, channel polling (webhooks get 503, so the platforms retry) and incoming peer messages (exchange envelopes wait in the inbox, the peer listener answers 503 so senders queue and retry), then waits for running tasks (`--timeout`, default 600 s). `--stop` then stops the server. `drain --off` cancels it. A task still running at the restart comes back as *interrupted* and can be resumed.
- **Migrations** run at start-up. Before the first real migration, the runtime settings are saved to `<data>/backups/pre-migrate-v<from>-v<to>-<UTC>.tar.gz`. A data dir written by a newer version stops start-up with a clear message.
- **Rolling back after a migration:** the old version refuses the newer data, so restore the backup from step 1 (`xqo backup restore ... --to NEW`) and start the old version on it.
- Several instances: upgrade one at a time, each with its own backup. Peers keep sending meanwhile; their messages are delivered when the upgraded instance is back.
