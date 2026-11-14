> **xqo-lite.** This build has only the core, the office plugin and the Claude Agent SDK, and stores events in
> jsonl. Parts about schedules, channels, feeds, peering, toolchains, the Docker sandbox, agency, sqlite or
> postgres don't apply here (see ../README.md). Missing plugins are startup warnings, not errors.

# MASTERPLAN: xqo, Python AI agents HQ

## Goal
A local xqo where Claude-powered agents:
1. Talk to each other directly (request → reply).
2. Broadcast a **skill lookup** ("who can do `X`?") and hand the task over to an agent that offers.
3. Have every fenced code block they produce extracted and saved as a **timestamped artifact**, per agent, per task.
4. Are observable live on a **local website** that streams the whole message flow and serves artifacts for download.

## Architecture

```
            ┌──────────────── Web UI (FastAPI + SSE) ────────────────┐
            │ live flow · agents · tasks · artifacts (file / zip)    │
            └───────────────▲───────────────────────┬────────────────┘
                     events │                        │ POST /api/tasks
┌───────────────────────────┴────────────────────────▼──────────────┐
│                         MessageBus (asyncio)                       │
│  direct()  broadcast()  offers  handover()  event log (jsonl)      │
└───────▲──────────────▲──────────────▲──────────────▲──────────────┘
        │              │              │              │
   Agent planner   Agent coder   Agent reviewer   Agent writer …
   (skills, system prompt, Claude tool loop, ArtifactStore hook)
                         │
                Anthropic Messages API
```

### Components (`xqo/`)
| Module | Responsibility |
|---|---|
| `bus.py` | Registry, direct messaging, broadcast skill lookup, handover, event fan‑out (SSE subscribers) and JSONL log. |
| `agent.py` | `Agent`: name, skills, system prompt, model. Runs a manual Claude tool‑use loop with orchestration tools. |
| `llm.py` | Anthropic client wrapper (real) and a scripted `MockLLM` for offline demos/tests. |
| `artifacts.py` | Extracts fenced blocks ```` ```lang ```` from every agent output, saves to `artifacts/<task_id>/<agent>/<UTC timestamp>_<n>.<ext>`, keeps a manifest. |
| `models.py` | `Event`, `Task` dataclasses. |
| `web.py` | FastAPI app: dashboard HTML, SSE stream, REST for agents/tasks/artifacts, zip download. |
| `config.py` | Loads `agents.json` (agent roster). |
| `__main__.py` | CLI: `python -m xqo serve` / `run "prompt"`. |

## Communication protocol
All traffic is an `Event` (`id, ts, kind, sender, recipient, task_id, parent_task_id, content, meta`) published on the bus and streamed to the UI.

| Kind | Meaning |
|---|---|
| `task` | User submits a task to an entry agent. |
| `direct` / `reply` | Agent A asks agent B (`ask_agent` tool); B runs its loop and replies. |
| `broadcast` | Agent A asks everyone "who has skill S?" (`handover_task` tool). |
| `offer` | Agents whose skills match answer (deterministic match, no LLM cost; load = active tasks). |
| `handover` | A picks the best offer (exact match → least busy) and hands the sub‑task over directly. |
| `result` | Sub‑task result returned to the requester (becomes the tool result). |
| `artifact` | A fenced block was extracted and saved. |
| `llm` / `error` / `done` | Model call stats, failures, task completion. |

Safety: max hop depth (`MAX_DEPTH`, default 4), an agent never hands over to itself or to an agent already in the task chain, offer window timeout (default 1.5 s).

## Agent tools (exposed to Claude)
- `list_agents()` — roster with skills.
- `ask_agent(agent, message)` — direct message, waits for reply.
- `handover_task(skill, task)` — broadcast skill lookup → handover → result.

## Model / API
- Anthropic Python SDK, `client.beta.messages.create`, default model `claude-opus-5-5` (per‑agent override in `agents.json`), adaptive thinking (default on), `output_config.effort` per agent.
- Server-side refusal fallback enabled (`fallbacks: "default"`, beta `server-side-fallback-2026-07-01`); `stop_reason` checked before use.
- Full `response.content` is appended back to history (keeps thinking blocks valid).
- `XQO_MOCK=1` runs a scripted fake LLM so the whole flow can be demoed without a key.

## Artifacts
- Every assistant text block (final and intermediate) is scanned.
- Path: `artifacts/<task_id>/<agent>/<YYYYMMDDTHHMMSS.ffffffZ>_<n>.<ext>` (ext from fence language).
- `artifacts/manifest.jsonl` records task, agent, timestamp, language, size, path.
- UI: per task/agent list, single file download, zip per task.

## Web UI
Single page (no build step): live event timeline with colour per kind, filter by task, agent roster with live load, submit‑task form, artifact browser with downloads.

## Milestones
1. Plan (this file).
2. Core: models, bus, artifacts, llm, agent.
3. Web + CLI.
4. Mock end‑to‑end test (handover + artifacts + SSE).
5. README.
6. Hardening (done): handover failover to next offer, per-task timeout, SDK retries, prompt caching, per-agent usage stats, history replay on restart, task-tree API + UI panel, SSE backfill on reconnect, input validation, path-traversal tests.

7. Named artifacts + patching (done): file-name detection across fence variants, per-task workspace tree with versioned history, unified diff / SEARCH-REPLACE / tool patching with failure feedback.

8. Task naming `task_<YYMMDDhhmm> : <title>` (done).
9. Attention flags (done): `ask_user` tool + automatic triggers (time limit, API retries, no offer, failing patches), `needs_attention` status, auto-confirm after 180 s, dashboard panel, browser notifications/sound/title, persisted events.
10. Dashboard (done): animated working-status graph, resizable panels, notes board, artifact viewer with highlighting, side-by-side diffs and rollback.
11. Platform (done): sandboxed `run_code` for a tester agent (Docker or local), shared notes board, MCP tools (remote connector + local stdio), packaging (pyproject, console script, Dockerfile, compose).
12. Git (done): a repo per task workspace (`artifacts/<task>/workspace/.git`), one commit per artifact version authored by the agent, `.git` hidden from agents, zips, sandbox and downloads; push on demand with a token read from the environment.
13. Replay and branching (done): full event log per task, step-by-step replay on the dashboard (graph and timeline), fork from any event with an edited prompt; the fork gets the workspace and notes board as they were at that event plus the earlier message flow as context.
14. Attachments (done): images, PDFs and text files on a task (file picker, drag and drop, clipboard paste; long text pastes become a .txt), stored under `artifacts/<task>/attachments/`, sent as Claude image/document content blocks, forwardable to sub-tasks by name, kept on forks.
15. Rich rendering (done): Markdown (markdown-it, GFM tables/task lists/strikethrough/footnotes/auto-links), KaTeX math, Mermaid diagrams (lazy, strict, SVG/PNG export), highlight.js, DOMPurify; in the Live viewer (raw toggle, collapse) and the artifact viewer (Source / Rendered / Split, table of contents). All libraries vendored under `static/vendor/`.
16. Departments / HQ (done): department files with heads, two-level skill broadcast (own department, then heads), `@department` targeting, contact rules, department-scoped sandbox and MCP, per-department notes board and workspace with `publish_files`, token/cost budgets with extend/stop, dashboard boxes/filters/stats, `--departments` and live reload.
17. Per-task venv (done): local test runs for Python projects use `artifacts/<task>/.venv`, made with uv (plain venv fallback), deps reinstalled only when they change.
18. Cancel and delete (done): cancel a running task (model calls dropped, questions closed, processes and containers killed, final commit, `task_cancelled` event); delete finished tasks to a trash with undo, purge after a retention time or on demand (folders, manifest rows and log events removed, `task_deleted` record kept, forks kept).
19. Talk during a task (done): New task | Ask | Steer tabs with an Urgent toggle; per-agent inboxes delivered at the next turn, urgent Ask answered from a side call, `reply_user` chat replies, chat answers pending questions, finished tasks reopen in place; pause/resume (calls finish, runs stop, timers freeze); test runs marked pass/fail with a flag after 3 failures in a row.
20. Provider per agent (done): `provider` = anthropic (default) / openai / ollama / named provider / OpenAI-compatible URL, inherited like the model; keys only from env vars named by `api_key_env`; an adapter maps tools, tool results, images and usage so the whole loop works the same; provider chip with key status on the Agents panel.
21. Avatars (done): optional `avatar` per agent and `user_avatar` (image file, URL or emoji) on the graph nodes and Agents panel.
22. Graph layouts (done): ring / grid / star / by department picker in the Flow graph header, remembered per browser, default `graph_layout` in agents.json.
23. History trimming (done): old tool results and large tool inputs replaced by stubs pointing to the saved artifact or file, in batches to keep prompt caching effective.
24. Output limit (done): `max_tokens` per agent / department / agents.json (default 32k), streamed Anthropic calls, cut-off replies continued up to 2 times then the user is asked, files extracted only from complete replies.

## Next ideas
- Streaming agent output token-by-token to the UI.
- LLM-based offer evaluation (agents judge fit, not just skill match).
- Auth on the web UI if exposed beyond localhost.
