# xqo-feeds: agents watch this machine

```bash
pip install xqo-feeds      # also installs xqo-schedules, which runs the rules' tasks
```

Agents can watch the machine the xqo runs on (CPU, memory, disk, processes, systemd services, files,
logs, the journal, Docker) and events that scripts, cron jobs, git hooks and CI push in. **Rules** decide what
reaches an agent, so most events never cost a model call.

```
 sources (no model)          rules (no model)                      actions
 system / process / service  when · for · clear · severity · kind  log     a feed_rule event only
 file / log / journald  ───▶ cooldown · caps · quiet hours  ─────▶ notice  an alert (dashboard, channel operators)
 docker / push (+ CLI)       group_by (incidents)                   digest  one task per window with all events
                                                                    wake    a task now; later events of the same
                                                                            incident go to that open task
```

Feeds are meant for a **native** install (the xqo sees the whole host). In Docker it sees only its own
container: see [Docker](#docker) below.

## Setup

```bash
pip install ".[feeds]"     # psutil (system / process feeds) and watchfiles (file feeds; polling without it)
```

```jsonc
"feeds": {
  "system":  {"type": "system", "every": "30s", "disks": {"root": "/", "data": "/srv"}},
  "web":     {"type": "process", "match": "gunicorn", "every": "30s"},
  "nginx":   {"type": "service", "units": ["nginx", "postgresql"]},
  "app-log": {"type": "log", "path": "/var/log/app/*.log", "pattern": "ERROR|CRITICAL|Traceback"},
  "journal": {"type": "journald", "units": ["app.service"], "priority": "err"},
  "uploads": {"type": "file", "paths": ["/srv/uploads"], "include": ["*.csv"]},
  "docker":  {"type": "docker", "events": ["die", "oom", "health_status"]},
  "backup":  {"type": "push", "token_env": "FEED_BACKUP_TOKEN"},
  "ci":      {"type": "push", "preset": "github", "secret_env": "GH_WEBHOOK_SECRET"},
  "listen":  {"socket": "/run/user/1000/xqo-feeds.sock"}   // optional: push only, on a Unix socket or port
},
"feed_rules": [
  {"name": "disk-full", "feed": "system", "when": "disk.percent > 90", "for": "5m", "clear": "disk.percent < 85",
   "action": "wake", "agent": "ops", "prompt": "Find what fills the disk and suggest what to delete."},
  {"name": "app-errors", "feed": "app-log", "severity": "error", "action": "digest", "every": "1h",
   "min_events": 3, "agent": "ops"},
  {"name": "backup-failed", "feed": "backup", "when": "status != 0", "action": "notice"},
  {"name": "ci-red", "feed": "ci", "when": "conclusion == 'failure'", "action": "wake", "agent": "coder",
   "group_by": "branch", "limits": {"max_cost_usd": 1}},
  {"name": "web-down", "feed": "web", "when": "running == 0", "for": "1m", "action": "wake", "agent": "ops",
   "mode": "act", "actions": ["restart-web"]}
],
"feed_actions": {
  "restart-web": {"command": ["sudo", "-n", "systemctl", "restart", "gunicorn"], "timeout": 60,
                  "description": "restart the web app"}
},
"feed_defaults": {"quiet_hours": "23:00-07:00", "max_wakes_per_hour": 4},
"agents": [
  {"name": "ops", "skills": ["operations"], "feeds": {"*": "wake", "web": "act"}},
  {"name": "coder", "skills": ["python"], "feeds": {"ci": "wake"}}
]
```

`xqo serve` starts the feeds together with the scheduler (only one process per data dir watches them).
`xqo feed status` prints feeds, rules and open alerts; the dashboard's **📡 feeds** button shows the same
with live values, sparklines, recent events, alerts to acknowledge, and per rule: mute for an hour, test-fire, and
its runs.

## Access (default: none)

Each agent's `"feeds"` is `"none"`, `"read"`, `"wake"` or `"act"`, for all feeds or per feed (names or globs):
`{"*": "read", "web": "act"}`. A department file can set `"default_feeds"` for its members.

| Access | Gives |
|---|---|
| `read` | the tools `feed_status` (current values, alerts, firing rules) and `feed_read` (recent events of one feed) |
| `wake` | `read`, and rules may start tasks for the agent (report mode) |
| `act` | `wake`, and rules in act mode may start tasks for it; the `feed_action` tool |

A rule that starts tasks for an agent without enough access stops start-up with a clear message.

## Report or act (per rule)

A task a rule starts runs in the rule's `"mode"` (default `feed_defaults.mode`, shipped as `"report"`), for the
whole task tree (helpers it asks or hands over to are held to the same mode):

- **report**: diagnose and report. The task keeps only reading tools: `feed_status`, `feed_read`, notes,
  `read_file` / `list_files` on its workspace, `ask_user`, `reply_user`, `ask_agent` / `handover_task`,
  `channel_send` (claude-sdk agents: Read, Grep, Glob). No writing, no `run_code`, no MCP servers.
- **act**: report's tools, plus the rule's `"tools"` (names or globs, e.g. `["run_code", "write_file",
  "mcp__ops__*"]`, or `"Bash"` / `"Edit"` for claude-sdk agents) and its `"actions"`. An action is a fixed command
  from `feed_actions` (a list, never a shell line), run on this machine by `feed_action`. With `"approve": true`
  (the default) each action waits for the user's **yes** in the dashboard (`feed_defaults.approval_timeout`,
  default 1 h, then it is not run), so such rules run attended.

## Rules

| Setting | Meaning |
|---|---|
| `name` | lowercase letters, digits, `.`, `_`, `-` |
| `feed` | a feed name or glob |
| `kind`, `severity` | only events of this kind (or list / glob), only this severity or higher (`debug` < `info` < `warning` < `error` < `critical`) |
| `when` | a condition on the event's fields, e.g. `disk.percent > 90`, `status != 0 and kind == 'nightly'`, `'Traceback' in text`, `severity >= 'error'`, `not running`. Only fields, values, comparisons, `in`, `and` / `or` / `not`. A missing field never matches. |
| `for` | the condition must hold this long (per event key) before the rule fires once |
| `clear` | the rule re-arms only when this is true (hysteresis); without it, when `when` is false again |
| `action` | `log`, `notice`, `digest` or `wake` |
| `agent`, `prompt` | who gets digest / wake tasks, and what to do (the event follows as data) |
| `mode`, `tools`, `actions`, `approve` | see above |
| `cooldown` | a rule fires at most once per key in this time (default `10m`; `0` = every match) |
| `every`, `min_events` | digest window (≥ 1m) and the fewest events worth a task (fewer are dropped) |
| `group_by` | a field that names the incident: while the task of that incident is open, new events go to it as messages instead of starting a task (without it: one incident per rule) |
| `max_wakes_per_hour`, `max_wakes_per_day` | new tasks per rule (defaults 4 and 20); more raise one alert and are only logged |
| `limits`, `max_failures`, `archive_after` | as for schedules: `{"max_turns", "max_tokens", "max_cost_usd"}` per run, pause after N failed runs, archive old runs |
| `quiet_hours`, `notify_channels`, `enabled`, `description` | per-rule overrides |

Each digest / wake rule appears in the dashboard's schedules as **📡 feed: name** (pause, resume, run now, runs);
its definition only changes in agents.json. Department budgets apply to its tasks like to any other.

`feed_defaults`: `mode`, `cooldown`, `max_wakes_per_hour`, `max_wakes_per_day`, `total_wakes_per_hour` (all rules,
default 10), `quiet_hours` (wakes wait and arrive as one digest when the quiet time ends; notices skip the
channels), `tz`, `approve`, `approval_timeout`, `notify_channels`, `max_events_per_minute` (events logged per feed,
default 60; more are kept and counted, not logged).

## Sources

| Type | Settings | Events |
|---|---|---|
| `system` | `every` (≥ 5s, default 30s), `disks` (list or `{name: path}`) | samples: `cpu.percent`, `mem.percent`, `mem.available_mb`, `swap.percent`, `load.1/5/15`, `procs`, `uptime_h`, `disk.percent` / `disk.free_gb` (first disk) and `disk.<name>.percent` |
| `process` | `match` (text in the name or command line, or a list) or `regex`, `every` | samples: `running`, `cpu.percent`, `mem.mb`, `pids`; events `started` / `stopped` |
| `service` | `units`, `every` | samples per unit (key = unit): `active`, `state`, `sub`, `restarts`, `result`; event `state` on a change (Linux, systemd) |
| `file` | `paths`, `include` / `ignore` globs, `poll` (+ `every`) | `added`, `modified`, `deleted` with `path`, `name`, `size` (watchfiles, else polling) |
| `log` | `path` (file or glob), `pattern` / `exclude` regex, `every`, `max_line`, `from_start`, `severity` | `line` with the text; severity from ERROR / WARN words; key = the line without numbers. Follows rotation; the read position survives restarts; the first start begins at the end of the file. |
| `journald` | `units`, `priority` (0-7 or `err`...), `pattern` | `journal` with `unit`, `priority`, text (Linux, `journalctl`) |
| `docker` | `socket` (default `/var/run/docker.sock` or `DOCKER_HOST`), `events`, `containers` (globs) | the action (`die`, `oom`, `health_status`...) with `container`, `image`, `exit_code`, `health` |
| `push` | `token_env` and/or `secret_env`, `preset` (`github`, `gitlab`), `max_body_kb` | what was POSTed; see below |

Common settings: `trust` (`metrics` for system / process / service / docker, `untrusted` for the rest: shown in
the dashboard), `max_events_per_minute`, `enabled`, `description`.

### Push: cron jobs, scripts, git hooks, CI

```bash
export XQO_FEED_TOKEN=...   # the value of the feed's token_env on the server
backup.sh; xqo feed emit backup --status $? --key nightly
make test 2>&1 | tail -50 | xqo feed emit ci-local --kind tests --text - --status ${PIPESTATUS[0]}
curl -H "Authorization: Bearer $TOKEN" -d '{"kind": "deploy", "fields": {"version": "1.4"}}' \
     http://127.0.0.1:8000/api/feeds/deploys/events
```

`POST /api/feeds/<name>/events` takes `{"kind", "severity", "key", "fields", "text"}` or a list of up to 50.
`token_env`: `Authorization: Bearer <token>`. `secret_env`: `X-Hub-Signature-256: sha256=<HMAC-SHA256 of the body>`.
Presets: `github` (the webhook's secret in `secret_env`; `workflow_run`, `check_suite`, `check_run`, `push`,
`pull_request`, `issues`) and `gitlab` (the secret token in `token_env`; pipeline, job, push, merge request). A push
feed without a token or secret is refused at start-up. `"listen"` serves only this route on its own port or a Unix
socket (mode 0600), so the dashboard can stay private when GitHub has to reach it.

## Safety

- Event text (log lines, file names, webhook bodies, commit messages) reaches agents between random markers as
  data, with a note never to follow it; secret-looking strings are redacted before anything is stored or shown.
- Report mode is the default; act mode needs `act` access, explicit tools / actions, and (by default) the user's
  approval for each action. Actions are fixed argument lists from agents.json; the agent only picks a name.
- Nothing in agents.json runs a shell command except `feed_actions`, and only from an act-mode task.
- Caps: cooldown per key, wakes per rule per hour / day, wakes of all rules per hour, logged events per minute,
  plus schedule limits and department budgets per run.

## Docker

In a container, `system` and `process` see the container, `service` and `journald` have no systemd, and `docker`
needs the socket. Feeds that work as they are: `log` and `file` on mounted paths (on Docker Desktop use
`"poll": true`) and `push` (publish the port on 127.0.0.1 only). For host coverage, prefer a small script on the
host that runs `xqo feed emit` (or curl) over widening the container's privileges.

## Builds

Builds with and without the Claude Agent SDK have the same feeds. With the SDK,
`feed_status`, `feed_read` and `feed_action` are served to Claude Code like the other xqo tools, and
report mode keeps only Read, Grep and Glob of Claude Code's own tools. Claude Code sessions cost more to start, so
prefer `digest` over `wake` for chatty feeds.
