> **xqo 3.0.** The `xqo` package has the core, the office plugin and the Claude Agent SDK, and stores events in
> jsonl. Schedules, channels, feeds, peering, toolchains, the Docker sandbox, agency, sqlite and postgres are
> separate plugin packages (`pip install xqo-<name>`, or `xqo[all]`); parts about them apply once they are
> installed. A plugin that is not installed is a startup warning, not an error.

# SETUP: running one or many HQs

This guide covers every way to run xqo: one instance, several isolated instances on one machine, and isolated instances that share messages and files (with **peering**) on one machine or across machines. Quick start and features are in [README.md](README.md).

| Variant | Isolation | Sharing | Go to |
|---|---|---|---|
| A. One instance | n/a | n/a | [A](#a-one-instance) |
| B. Several instances, nothing shared | separate processes or containers | none | [B](#b-several-isolated-instances) |
| C. Several instances on one machine, sharing | same as B | shared exchange folder | [C](#c-sharing-on-one-machine-exchange-folder) |
| D. Instances on different machines, sharing | separate hosts | HTTP peer API with tokens | [D](#d-sharing-across-machines-http-peer-api) |
| E. Mixed | both | exchange locally, HTTP to remote hosts | [E](#e-mixed-topologies) |

## What one instance owns

Every xqo instance is one process. It keeps all of its state in its own data dir, so two instances never share anything unless you set up peering.

| Setting | CLI flag / env var | What it isolates |
|---|---|---|
| Data dir | `--data-dir` / `XQO_DATA` | `artifacts/` (workspaces, git repos, history, attachments, notes, trash, archive, schedules, limits override, peer log) and `logs/events.jsonl` |
| Port | `serve --port` / `XQO_PORT` (`--host` / `XQO_HOST`) | Dashboard and API |
| Roster | `--config` / `XQO_CONFIG` | HQ agents, models, providers, budgets' prices, git, sandbox, peering |
| Departments | `--departments` / `XQO_DEPARTMENTS`, `--departments-dir` / `XQO_DEPARTMENTS_DIR` | Which departments (and agents) this instance has |
| Sandbox run copies | `XQO_RUN_DIR` (or `sandbox.run_dir`) | Temporary run folders |
| API keys | `ANTHROPIC_API_KEY`, provider `api_key_env` | Who pays for the calls |
| Git push token | `XQO_GIT_TOKEN` (or `git.push_token_env`) | Where workspaces can be pushed |
| Peer name | `peering.name` / `XQO_PEER_NAME` | How other instances address this one |

Department budgets, the *No limits* hour, the trash, the archive, the schedules and the agency imports (`agency.json` next to `agents.json`) are per instance too.

Messaging channels (`"channels"` in the config, see [CHANNELS.md](CHANNELS.md)) are per instance as well: give each instance its own bot, number or account. Two instances polling the same Telegram bot, X account, mailbox or Twilio number would take each other's messages, and their conversation state lives in each data dir (`artifacts/channels/`).

Local feeds (`"feeds"`, see [FEEDS.md](FEEDS.md)) are watched by the instance that runs the scheduler of its data dir. Instances on one machine each see the same CPU, disks and logs, so give the system-wide rules to one of them; push feeds and `feeds.listen` ports need different names or ports per instance.

Schedules run only in the instance that owns them, so nothing fires twice across instances; the `serve` process takes `artifacts/scheduler.lock`, and a second process pointed at the same data dir by mistake does not run them (see README → Scheduled and repeating tasks).

Rule of thumb: **one folder per instance**, holding its `agents.json`, `departments/`, and data. Never point two running instances at the same data dir.

### One master config file per instance

All the settings above can live in the instance's config file, so you start it with one argument:

```bash
xqo hq/eng/agents.json            # = xqo --config hq/eng/agents.json serve
xqo hq/eng/agents.json peers      # any command works after the file: run, peers, cancel, agency, ...
```
Use the `"server"` block for what used to be command-line options. Everything else (models, providers, budgets' prices, git, sandbox incl. `run_dir`, trash, peering) is already in the file:
```json
{
  "server": {"host": "127.0.0.1", "port": 8001, "data_dir": "data", "departments": ["engineering"], "mock": false},
  "departments_dir": "departments",
  "sandbox": {"backend": "auto", "run_dir": "runs"},
  "peering": {"name": "eng", "exchange_dir": "/srv/xqo-exchange", "peers": {"marketing": {}}},
  "agents": [ ... ]
}
```

| `server` key | Same as | Default |
|---|---|---|
| `host` | `serve --host` / `XQO_HOST` | `127.0.0.1` |
| `port` | `serve --port` / `XQO_PORT` | `8000` |
| `data_dir` | `--data-dir` / `XQO_DATA` (relative to the config file's folder) | the current folder |
| `departments` | `--departments` / `XQO_DEPARTMENTS`: a list, `"a,b"`, `"all"` or `"none"` | all |
| `mock` | `--mock` | `false` |
| `url` | `--url` / `XQO_URL` for client commands (`peers`, `ask`, `cancel`, …) | `http://<host>:<port>` |

### Workspace and attachment limits

The top-level `"limits"` block in the same file sets the size limits of the instance itself. These apply to agents, dashboard and CLI uploads, and files received from peers alike:
```json
"limits": {"workspace_file_mb": 5, "image_mb": 5, "pdf_mb": 20, "text_attachment_mb": 2,
           "attachments_total_mb": 24, "attachments_max_files": 20}
```

| Setting | Meaning | Default | Env override |
|---|---|---|---|
| `workspace_file_mb` | One text file an agent writes or patches in a workspace | 5 | `XQO_WORKSPACE_FILE_MB` |
| `image_mb` | One image attachment | 5 | `XQO_IMAGE_MB` |
| `pdf_mb` | One PDF attachment | 20 | `XQO_PDF_MB` |
| `text_attachment_mb` | One text-file attachment | 2 | `XQO_TEXT_ATTACHMENT_MB` |
| `attachments_total_mb` | All attachments of one task or message | 24 | `XQO_ATTACHMENTS_TOTAL_MB` |
| `attachments_max_files` | Attachments per task or message | 20 | `XQO_ATTACHMENTS_MAX_FILES` |

- Sizes are decimal MB, and fractions are allowed. Unknown keys, non-numbers and zero stop startup with a clear error.
- The dashboard picks the limits up and checks files before uploading them.
- Images and PDFs are sent to the model inside the API request, which has its own limits (about 5 MB per image and 32 MB per request). Above `image_mb` 5, `pdf_mb` 32 or `attachments_total_mb` 24, the server prints a warning at startup, and model calls with such files may be refused. Agents that use file references (`read_attachment`) only load a file when they open it.
- These are separate from the dashboard's **No limits** checkbox, which only lifts `max_turns` / `max_tokens` for an hour, and from `peering.limits`.

**Precedence**: command-line flag, then environment variable, then the config file, then the built-in default. In Docker, the image sets `XQO_DATA`, `XQO_HOST` and `XQO_PORT`, so the container's own `/data`, host and port always apply; map ports in Compose instead. `xqo init` writes a starter file that already has a `server` block. Secrets (API keys, tokens) never go in the file: it only names the environment variables that hold them.

---

## A. One instance

```bash
uv venv && source .venv/bin/activate && uv pip install .
export ANTHROPIC_API_KEY=sk-ant-...
xqo init            # agents.json + departments/ to edit
xqo serve           # http://127.0.0.1:8000
```
With Docker: `docker compose up --build` (repository root; data in `./data`).

**Claude Agent SDK backend**: always installed with xqo (`pip install xqo`; Docker: `docker compose up --build`) and every agent runs as a Claude Code session by default; sign in with `ANTHROPIC_API_KEY` or the local `claude` login. Each instance is a separate process with its own sessions, so the same applies per instance in B–E. Peering is the same with either backend: `list_peers` and `send_to_peer` reach Claude Code through the in-process `xqo` MCP server, and files that Claude Code writes with Write / Edit can be sent like any workspace file.

---

## B. Several isolated instances

Give each instance its own folder, port and department set. They don't know about each other.

### B1. Plain processes (uv)

```bash
uv venv && source .venv/bin/activate && uv pip install .
for n in eng marketing research; do mkdir -p hq/$n && xqo init hq/$n/agents.json; done
# then edit each hq/<name>/agents.json: "server": {"port": 8001, "departments": ["engineering"]} (8002 marketing, 8003 research)
# and "sandbox": {..., "run_dir": "runs"}; trim hq/<name>/departments/ if you like

xqo hq/eng/agents.json &
xqo hq/marketing/agents.json &
xqo hq/research/agents.json &
```
Data goes next to each file (`"data_dir": "."` from `init`), and `departments/` next to it is used. The same with flags only: `(cd hq/eng && xqo --data-dir . --departments engineering serve --port 8001)`. For a separate bill per instance, export a different `ANTHROPIC_API_KEY` in each shell, or give each `agents.json` its own `providers` entry with its own `api_key_env`.

CLI commands that talk to a server find it from the file: `xqo hq/marketing/agents.json peers`. Otherwise use `--url` or `XQO_URL=http://127.0.0.1:8002`.

### B2. Docker Compose (recommended)

[`deploy/multi/docker-compose.yml`](deploy/multi/docker-compose.yml) runs three instances (`eng`, `marketing`, `research`) on ports 8001 to 8003, each with its own `/data` folder. Each one's department set is `server.departments` in its `agents.json`.

```bash
cd deploy/multi
export ANTHROPIC_API_KEY=sk-ant-...
docker compose up --build
```
Each `deploy/multi/<name>/` folder is that instance's `/data`. Its `agents.json` is used, artifacts and logs are written there, and you can add a `departments/` folder to replace the packaged ones. The example files already contain a `peering` block (variant C). To run the instances fully isolated, delete the `peering` block and the `exchange` volume.

Adding an instance: copy a service block and a `<name>/agents.json`, then change the name, host port, `server.departments`, the peering name and the `/data` folder.

> The container user must be able to write the bind-mounted folder. If you see permission errors, run `sudo chown -R 1000:1000 deploy/multi/<name>` (or the uid of `app` in the image).

### B3. systemd, Kubernetes and others

The same rule applies on any platform: one unit, pod or service per instance, each with its own data volume, port, department set (`server.departments`) and secrets. In Kubernetes, use one Deployment with `replicas: 1` and its own PersistentVolumeClaim per instance. Don't scale one instance to more than one replica: the data dir is single-writer.

---

## C. Sharing on one machine: exchange folder

Instances on the same host (or the same shared volume) exchange **messages** and **workspace files** through one shared folder. Each instance has an inbox there, `<exchange>/<name>/inbox/`, and polls it every 2 seconds.

Add a `peering` block to each `agents.json`. **Only the peers listed there can send to or receive from this instance.**

`eng/agents.json`:
```json
"peering": {
  "name": "eng",
  "exchange_dir": "/exchange",
  "peers": {
    "marketing": {"description": "marketing HQ: copy, launches, campaigns"},
    "research":  {"description": "research HQ: analysis and reports", "receive": ["messages"]}
  }
}
```
`marketing/agents.json`:
```json
"peering": {"name": "marketing", "exchange_dir": "/exchange", "peers": {"eng": {}}}
```
`research/agents.json`:
```json
"peering": {"name": "research", "exchange_dir": "/exchange", "peers": {"eng": {"send": ["messages"]}}}
```
Every instance points at the same folder: the `exchange` volume in Compose, or a host path such as `"exchange_dir": "/srv/xqo-exchange"` with plain processes. `XQO_EXCHANGE_DIR` overrides it. Restart the instances after changing `peering`.

**Optional signing**: on a folder that other users or programs can write to, give each pair a shared secret. Generate it with `xqo peers token`, set it on both sides as an environment variable, and name that variable with `"token_env"` in each side's entry for the other. Envelopes are then signed with HMAC-SHA256, and unsigned or altered ones are rejected.

### How sharing works
- **Agents**: by default HQ agents and department heads get two tools. `list_peers` shows who they can reach. `send_to_peer(peer, message, files?, want_reply?, reply?)` sends a message and/or files from the task workspace. Members of departments do not get these tools; they ask their head.
- **An incoming message starts a task** on the receiving instance, with its entry agent: `peering.entry_agent`, or per peer `"entry_agent"`, else the first agent. Files land in that task's workspace under `peers/<sender>/<path>`.
- **Replies**: with `want_reply: true`, the receiver's final answer goes back automatically when its task ends. It arrives in the sender's original task: queued for its agents while the task runs, or the task is reopened when it has finished. The receiving task can also send files back with `send_to_peer(..., reply=true)`. Files from a reply land under `peers/<sender>/` in the original task.
- **Loop protection**: every hop of a chain (A → B → C) counts. A chain longer than `max_hops` (default 3) is refused. Replies don't add a hop.
- **Duplicates**: an envelope id that was already accepted is acknowledged and not run again, so retries are safe.
- **What can be shared**:
  - Any workspace file, text or binary. Text travels as text. Anything else (images, PDFs, archives, models, …) travels base64-encoded and is checked with SHA-256. It is saved unchanged under `peers/<sender>/`, with history and a git commit. Images and PDFs among the files are also attached to the receiving task, so its agents can look at them with `read_attachment`. Other binary files can't be read as text by agents, but they can be downloaded from the dashboard and sent on.
  - The task's **attachments** (images, PDFs, text files): use `send_to_peer(..., attachments=[names])`, `--attachment` on the CLI, or `attachments` in the API. They arrive as attachments of the receiving task, or of the original task for a reply.
  - Limits (defaults, all settable, see [Limits](#limits)): 20 files, 20 MB per file and 20 MB per envelope, and 100,000 characters of message text. Text files over the workspace text limit (`limits.workspace_file_mb`, 5 MB) travel as bytes. Attachments also follow the [workspace and attachment limits](#workspace-and-attachment-limits) (images 5 MB, PDFs 20 MB by default).
- **The user can send too**: use the dashboard API (`POST /api/peers/send`) or `xqo peers send <peer> "text" [--task <id> --file <path> --attachment <name> --want-reply]`.

---

## D. Sharing across machines: HTTP peer API

For instances on other hosts, each receiving instance opens a **separate peer listener**. It serves only `/peer/v1/inbox` and `/peer/v1/ping`, never the dashboard, so the dashboard can stay on `127.0.0.1`. Each pair of instances shares one bearer token.

1. **Make a token** for the pair, on either machine: `xqo peers token`. Put it in the same environment variable name on both machines, e.g. `PEER_LAB_TOKEN`.
2. **Receiver** (`lab`, host 10.0.0.7):
   ```json
   "peering": {
     "name": "lab",
     "listen": "0.0.0.0:8100",
     "peers": {"eng": {"url": "http://10.0.0.5:8100", "token_env": "PEER_LAB_TOKEN"}}
   }
   ```
3. **Sender** (`eng`, host 10.0.0.5):
   ```json
   "peering": {
     "name": "eng",
     "listen": "0.0.0.0:8100",
     "peers": {"lab": {"url": "http://10.0.0.7:8100", "token_env": "PEER_LAB_TOKEN"}}
   }
   ```
4. Start both with the token in the environment (`PEER_LAB_TOKEN=... xqo serve`). The log shows `Peer API: http://0.0.0.0:8100/peer/v1/inbox`. With Docker, publish the port (`"8100:8100"`) and pass the variable.
5. Check: `xqo peers` lists the peers, and flags a token variable that is not set.

**One-way links**: if only `eng` sends to `lab`, then `eng` needs no `listen`, and `lab` lists `eng` with `"send": false`, its `token_env` and no `url`. Replies travel like messages. For `lab` to answer back, `eng` needs `listen`, `lab` needs `eng`'s `url`, and `lab`'s `send` must allow messages.

**When a peer is down**: an HTTP send that fails with a connection error, 429 or 5xx is queued in `artifacts/.peers/outbox/` and retried every 30 seconds for up to 24 hours. The dashboard shows the queued count. A 4xx answer (bad token, not allowed, too large) is not retried; the agent or user gets the reason straight away.

**Securing the link** (pick one):
- **Private network**: Tailscale, WireGuard or a VPN, with `listen` bound to the private interface (e.g. `"100.64.0.3:8100"`).
- **TLS reverse proxy**: Caddy or nginx in front of the listener, with `"url": "https://lab.example.com"` on the sender. For example, a Caddyfile line `lab.example.com { reverse_proxy 127.0.0.1:8100 }` with `"listen": "127.0.0.1:8100"`.
- **SSH tunnel**: `ssh -N -L 8107:127.0.0.1:8100 lab-host` and `"url": "http://127.0.0.1:8107"` on the sender.

Plain HTTP on an open network sends the token and content in clear text. Use one of the options above outside a trusted LAN, and firewall the peer port to the peers' addresses.

---

## E. Mixed topologies

One instance can use both transports. Peers without `url` go through the exchange folder; peers with `url` go over HTTP:

```json
"peering": {
  "name": "eng",
  "exchange_dir": "/exchange",
  "listen": "0.0.0.0:8100",
  "peers": {
    "marketing": {},
    "lab": {"url": "https://lab.example.com", "token_env": "PEER_LAB_TOKEN", "send": ["messages"]}
  }
}
```
There is no forwarding: every pair that should talk must list each other directly. Common shapes:
- **Hub and spoke**: one coordinating HQ lists every other instance; the others list only the hub.
- **Pairs**: only the instances that work together list each other.
- **Mesh**: everyone lists everyone. This is fine for a few instances, and `max_hops` stops runaway chains.

---

## Sharing policy reference

`"peering"` in `agents.json`:

| Field | Meaning | Default |
|---|---|---|
| `name` | This instance's id (letters, digits, `_ . -`). Env `XQO_PEER_NAME` wins | required |
| `exchange_dir` | Shared folder for peers without `url`. Env `XQO_EXCHANGE_DIR` | none |
| `listen` | `host:port` for the HTTP peer listener. Env `XQO_PEER_LISTEN` | none (no listener) |
| `agents` | Who gets `list_peers` / `send_to_peer`: `"heads"` (HQ agents and department heads), `"*"`, or a list of agent names | `"heads"` |
| `entry_agent` | Agent that takes incoming messages | first agent |
| `max_hops` | Longest peer chain accepted or started | 3 |
| `poll_seconds` | Exchange inbox polling interval | 2 |
| `limits` | Size, count and retry limits, see [Limits](#limits) | see there |
| `enabled` | `false` turns peering off (or env `XQO_PEERING=0`) | true |
| `peers` | `{name: {...}}`; only listed peers can send or receive | `{}` |

Per peer:

| Field | Meaning | Default |
|---|---|---|
| `send` | What this instance may send it: `true`, `false`, or a list of `"messages"`, `"files"` (a reply counts as a message) | `true` |
| `receive` | What this instance accepts from it (same values) | `true` |
| `url` | HTTP address of its peer listener. Without it, the exchange folder is used for sending | none |
| `token_env` | Env var holding the pair's shared token. Required for HTTP both ways; enables signing on the exchange folder. A plaintext `token` is refused | none |
| `entry_agent` | Agent that takes messages from this peer | `peering.entry_agent` |
| `description` | Shown to agents in `list_peers` and their system prompt | "" |

Examples:
- Accept only messages, never files: `"receive": ["messages"]`.
- Hand out files but never take work: `"send": ["files"], "receive": false`.
- Let only the planner contact peers: `"agents": ["planner"]`.
- Route a partner's requests to a department head: `"entry_agent": "mkt_lead"`.

### Limits

Each instance sets its own limits in `peering.limits`. A sender refuses to send more than its own limits allow, and a receiver refuses more than its limits allow. Rejected envelopes land in `rejected/` with a reason naming `peering.limits`.
```json
"peering": {
  "name": "eng",
  "limits": {"max_files": 40, "max_file_mb": 200, "max_total_mb": 500,
             "max_text_chars": 100000, "retry_seconds": 30, "queue_hours": 24}
}
```

| Setting | Meaning | Default | Env override |
|---|---|---|---|
| `max_files` | Files and attachments per envelope | 20 | `XQO_PEER_MAX_FILES` |
| `max_file_mb` | One file (decimal MB; fractions allowed) | 20 | `XQO_PEER_MAX_FILE_MB` |
| `max_total_mb` | All files of one envelope | 20 | `XQO_PEER_MAX_TOTAL_MB` |
| `max_text_chars` | Message text | 100000 | `XQO_PEER_MAX_TEXT_CHARS` |
| `retry_seconds` | Wait between delivery attempts of queued HTTP sends | 30 | `XQO_PEER_RETRY_SECONDS` |
| `queue_hours` | Queued HTTP sends older than this are dropped and reported | 24 | `XQO_PEER_QUEUE_HOURS` |

- The HTTP peer listener accepts request bodies up to about `max_total_mb` × 4/3, plus room for the text, because base64 makes binary data a third bigger. It follows the setting automatically.
- Set matching limits on both ends of a link: the smaller of the two applies in practice.
- Large envelopes are held in memory while they are sent and checked. For files of hundreds of MB or more, share a storage location (S3, a NAS path) and send the link as a message instead.
- The workspace text-file limit and the attachment limits are separate settings: see [Workspace and attachment limits](#workspace-and-attachment-limits).

---

## Operating it

- **Dashboard**: the **⇆ peers** button (shown when peering is on) lists the peers, their transport, what may be sent or received, token status and queued sends, plus the recent messages in both directions with links to their tasks. Peer activity appears as `peer` events in the Live viewer.
- **CLI**: `xqo peers` (status and recent messages), `xqo peers send ...`, `xqo peers token`. Use `--url` or `XQO_URL` to pick the instance.
- **API** (dashboard port): `GET /api/peers`, `GET /api/peers/messages?limit=`, `POST /api/peers/send {peer, text?, files?, attachments?, task_id?, want_reply?}`. `GET /api/config` includes `peering`.
- **Peer API** (listener port): `POST /peer/v1/inbox` and `GET /peer/v1/ping`, both with headers `X-XQO-Peer: <sender>` and `Authorization: Bearer <token>`.
- **Files on disk**:
  - `artifacts/.peers/messages.jsonl` logs every message in and out, and is used to rebuild reply routing after a restart.
  - `artifacts/.peers/outbox/` holds HTTP sends waiting to be retried.
  - `<exchange>/<name>/inbox/` holds new envelopes, `processed/` the handled ones, and `rejected/` those refused (the reason is in the log and the Live viewer).
  - Clean `processed/` from time to time; nothing is deleted automatically.
- **Drain, backup and upgrade** (see [OPERATIONS.md](OPERATIONS.md)): while an instance drains or writes a backup it leaves new envelopes in its exchange `inbox/` and its peer listener answers HTTP 503, so senders queue and retry; nothing is rejected. `artifacts/.peers/` (message log, outbox, reply routing) is part of `state` and `full` backups. After `xqo backup restore ... --to DIR`, keep `"server": {"data_dir": "."}` in the restored agents.json (the data is restored next to it) or pass `--data-dir DIR`.

## Opening task folders from the dashboard

**📁** on a task's artifacts shows its folders with **copy** and **open ↗** (editor) buttons. What to set depends on where xqo runs:

| Where xqo runs | Set | Result |
|---|---|---|
| Natively on your computer | nothing | Real paths; **open ↗** opens VS Code locally |
| Docker on your computer | `XQO_PATH_MAP=/data=<host folder bound to /data>` | Host paths; **open ↗** opens VS Code locally |
| Remote machine (plain or Docker) | `XQO_SSH_HOST=user@host`, plus `XQO_PATH_MAP` when in Docker | Remote paths; **open ↗** opens VS Code Remote-SSH on that folder |

The same settings go in agents.json as `"folders": {"path_map": {...}, "ssh_host": "...", "editor": "vscode"}`; the environment variables win. `XQO_EDITOR=none` keeps only **copy**. Remote-SSH needs the host in your SSH config (or reachable as `user@host`). Without access to the machine, use the **source ⬇** / **workspace ⬇** / **all ⬇** zips (**source ⬇** leaves out dependency and build folders).

## F. Office view and the planners world

Each dashboard has an **Office view** at `/office` (🏢 in the header; its title is the HQ name, `"name": "Acme HQ"` at the top of agents.json, which also replaces "HQ" in the dashboard): its departments and agents drawn as a 3D low-poly office (or the flat 2D pixel office: **3D / 2D** in the header), with a side panel to answer requests, Ask, Steer and give an agent a new task. It needs no setup.

The **planners world** (`/office?world=planners`, 🤝) puts the planners of several HQs in one room. List the other HQs' dashboards in each instance that should show it:

```json
"office": {
  "name": "hq-a",
  "planners": ["planner"],
  "hqs": [
    {"name": "hq-b", "url": "http://127.0.0.1:8002"},
    {"name": "hq-c", "url": "http://10.0.0.5:8003"}
  ]
}
```

| Setting | Meaning | Default |
|---|---|---|
| `name` | This HQ's label in the planners world | `peering.name`, else `hq` |
| `planners` | Agents of this HQ shown there | HQ agents named `planner` or with the `planning` skill |
| `hqs` | Other HQs' **dashboard** addresses (not their peer listeners); `XQO_OFFICE_HQS="hq-b=http://…;hq-c=http://…"` wins | none |

- Use the same names as in `peering` so a peer message between two HQs shows as one planner walking to the other.
- The browser only talks to the dashboard it opened. That dashboard relays to the HQs in `hqs`, and only these calls: read agents, departments, tasks, requests and live events; submit a task; Ask / Steer / pause / resume a task; answer a request. Anything else gets 403, and addresses that are not listed can't be reached.
- The relay needs network access from this instance to the other dashboards. Dashboards on other machines usually listen on `127.0.0.1` only (see Security notes); reach them over an SSH tunnel or a private network rather than opening them up. An HQ that can't be reached shows as *offline* in the room and in the header.
- With Docker Compose, use the service names, and `link` for the address your browser uses ("open in dashboard"): `{"name": "marketing", "url": "http://marketing:8000", "link": "http://127.0.0.1:8002"}`.

### Editing the office

The office is laid out automatically: a room per department (a desk per agent, a whiteboard, a plant and a bookshelf), windows on the back wall and a lounge along the bottom. **✎ edit** in the 3D view changes it, when the server was started with `--edit` (`xqo serve --edit`, or `xqo hq.json serve --edit`; in Docker add `--edit` to the command in docker-compose.yml). Without it the office can be looked at but not changed: the button is hidden and the server answers 403 to every layout or model change; what you change is saved for this HQ in `<data dir>/artifacts/office.json` and laid over the automatic layout, so when agents or departments are added they still get their desk and room, and your changes stay. In Docker that is on the `/data` volume, so it survives rebuilds. **automatic office** in the editor (then Save) goes back to the automatic layout; so does deleting the file.

- **Moving, turning, hiding**: automatic props are known by a stable name (`desk:<agent>`, `board:<department>`, `plant:<department>`, `shelf:<department>`, `window:<n>`, `coffee_bar`, `rug`, `beanbag_l`…); a desk takes its chair along and its agent sits facing it however it is turned. Positions count from the top-left of the prop's room (or of the lounge, or of the building), so a room keeps its furniture when the automatic layout moves it.
- **Adding**: the catalog is `xqo/plugins/office/static/office3d/catalog.json` (Kenney models and built-in shapes). An added prop belongs to the room it is dropped in. A prop with a *work* spot (a desk) can be given to an agent ("Who sits here"); *rest*, *social* and *utility* spots (beanbags, sofas, tables, the coffee machine) are where idle and paused agents go.
- **Paths**: in Edit mode the floor shows where agents can walk (red: blocked by furniture or the wall, green: corridor, coloured squares: the spots agents use, magenta: spots they cannot reach). **no-go** and **corridor** in the Paths section paint tiles by dragging over the floor (**erase paint** removes it, Esc goes back to moving furniture): agents never step on a no-go tile and prefer corridors. Paint is kept per room, like props. Idle agents wander between **waypoints** (`wander:lounge`, `wander:hall`…, and any you add from *Add › paths*); they show only while editing. Partition walls block, a **doorway** does not. The panel warns when an agent's desk (or another spot) cannot be reached. Agents head for different tiles, walk around agents who stand in their way and step aside for those walking past. In 2D, no-go paint and waypoints apply; corridors are 3D only.
- **Your own models**: upload a binary glTF (`.glb`) in the editor: [Kenney](https://kenney.nl/assets) kits (CC0, same style), [Blockbench](https://www.blockbench.net) for low-poly props and characters, or [Blender](https://www.blender.org) (File → Export → glTF 2.0, format *glTF Binary*). Give its footprint in tiles; without a `scale` it is fitted to that footprint. Uploaded files are kept in `artifacts/office-assets/` (up to 20 MB each, 200 files) and listed in `office.json`'s own `catalog`. Characters with Kenney's animation names (`idle`, `walk`, `sit`, `emote-yes`) move like the others; a model without them stands still.
- **Sharing**: **export** downloads `office.json`; **import** loads one (checked first, then Save). Uploaded models are not inside that file: upload them on the other HQ too (they show as pink boxes until then).
- **Two editors**: saving checks that nobody saved in between (another tab or person); if someone did, Discard loads theirs. Pages that only watch the office pick up a save within about 10 seconds.
- **By hand**: `office.json` can also be written by hand or by a script; the format is described in `validate_layout` in `xqo/plugins/office/view.py`. A file with a mistake is ignored (the office opens automatic and the editor says why).

```json
{"version": 1,
 "rooms": {"engineering": {"size": [14, 10]}},
 "props": {"desk:coder": {"x": 6, "y": 2, "rot": 90}, "plant:hq": {"hide": true}},
 "added": [{"uid": "u-1", "type": "sofa", "zone": "@lounge", "x": 10, "y": 3, "rot": 0}],
 "agents": {"coder": {"character": "character-male-c"}},
 "paint": {"engineering": {"block": [[0, 5], [1, 5]], "path": [[4, 6], [5, 6]]}},
 "theme": {"floor": "#b9bec6"}}
```

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `config error: peering.name ... must be set` | Add `"name"` or set `XQO_PEER_NAME` |
| `... may be sent to but have no url, so exchange_dir must be set` | Give that peer a `url`, set `exchange_dir`, or make it receive-only with `"send": false` |
| Message lands in `rejected/` with *not a peer* | The receiver doesn't list the sender under `peers`, or the names differ |
| *does not accept files / messages* | The receiver's `receive` for that peer doesn't include it |
| *bad or missing signature* | `token_env` is set on one side only, or the token values differ |
| HTTP 401 / 403 | The token differs, the variable isn't set on the receiver, or the sender's name isn't listed there |
| Sends stay *queued* | The peer listener is unreachable: check `listen`, published ports, the firewall and the `url` |
| *file too large ... (peering.limits ...)* | Raise `peering.limits` on that instance (the sender's or the receiver's, as named) |
| *peer chain too long* | Raise `max_hops` on both sides, or break the chain |
| Agents don't see the peer tools | They aren't HQ agents or department heads: set `"agents"`. Also check the agent's `tools` / `default_tools` policy does not deny `list_peers` / `send_to_peer` |
| Permission denied on `/exchange` or `/data` | Rebuild the image (it creates `/exchange` for the `app` user) and `chown` bind-mounted folders |

## Security notes

- Isolation between instances comes from separate processes or containers, data dirs and keys. Peering is the only channel between them, and it is opt-in on both sides.
- The dashboard has no login. Keep it on `127.0.0.1`, and expose only the peer listener.
- Like everything on the dashboard, editing the office and uploading models needs no login: whoever can open the dashboard can change them. Uploads are checked to be `.glb` / PNG / JPEG files and are served with `nosniff` and a sandbox policy. The office relay never forwards edits to another HQ.
- The office relay (`/api/office/hq/...`) only reaches the dashboards listed in `office.hqs`, and only forwards the calls the Office view makes. It adds no login of its own, so the same rule applies: whoever can open this dashboard can act on those HQs through it.
- Tokens are read from the environment and are never written to config, logs or events.
- Received files are verified against their SHA-256 checksum, and names are cleaned so they can't escape `peers/<sender>/`.
- Received content is data for the receiving agents. It starts a task with the receiver's own agents, tools, budgets and sandbox, never anything on the sender's side.
