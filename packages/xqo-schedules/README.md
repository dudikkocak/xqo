# xqo-schedules

Scheduled and repeating tasks for [xqo](https://github.com/dudikkocak/xqo): once, every N minutes / hours / days,
daily or weekdays at given times, or any 5-field cron expression, in a time zone of your choice. Feed rules
(`xqo-feeds`) run their tasks through it too.

```bash
pip install xqo-schedules

xqo schedule add "Nightly check" "Build and test, report failures" --cron "0 2 * * 1-5" --tz Europe/Berlin
xqo schedule list     # also: pause | resume | run | rm | runs <schedule_id>
```

The scheduler runs once per data dir, inside `xqo serve` (a `scheduler.lock` file keeps a second server on the same
data dir from running it too; backups leave the lock out). Its state is in `/api/plugins` under `schedules`.

## Details

**Scheduled and repeating tasks**: **⏱ schedules** in the Live viewer. A schedule starts an ordinary task (same path as **Submit**) on a timetable: **once** at a date and time, **every** N minutes / hours / days, **daily / weekdays** at given times, or any 5-field **cron** expression, in a time zone of your choice. Each run is named `<schedule> #<n>`, shows **⏱** in the task list and a badge in the Live viewer, and can be replayed, forked, archived or deleted like any task. Upcoming runs stay in the Schedules panel: next and last run, run count, **▶ Run now**, **Edit**, **History** (started / skipped / missed / finished runs with links to their tasks), pause / resume and **Delete** (tasks already started are kept). Per schedule:
- **Overlap**: if the previous run is still running, paused or waiting on you, the new one is *skipped* (default, logged), *queued* (one run starts when the previous one ends) or run *in parallel*.
- **Missed runs** (server down): *run once* at start-up if no older than the grace (default 1 h), or *skip*. Missed runs never pile up.
- **Limits**: max turns and max output tokens for that run's task tree, and a **max cost per run** that cancels the run when its estimated cost reaches it (Claude Agent SDK sessions report their cost when they end, so a run on the SDK path is stopped after the session that crossed the cap). The **No limits** hour does not apply to scheduled runs unless you tick it for the schedule. hq department budgets stay in force.
- **Unattended** (default): questions to you (ask_user, budget, time limit, …) take their default answer at once instead of waiting for the attention timeout (`auto_confirmed` by `unattended`). Untick it to have runs wait like any task.
- **Failures**: after 3 failed runs in a row (error, cost cap, cancelled by anyone but you; configurable, 0 = never) the schedule pauses itself and the dashboard shows a notice. **Stop after** N runs or a date; **archive finished runs after** e.g. `7d`.
- Schedules made in the dashboard, API or CLI live in `artifacts/schedules.json` with their run state and survive restarts; attachments are copied into every run. `"schedules": [...]` in `agents.json` adds schedules kept with the config (🔒 in the panel: pause, resume and run there, edit in the file):
  ```json
  "schedules": [{"name": "Daily report", "agent": "planner", "prompt": "Summarize yesterday's work",
                 "cron": "0 7 * * 1-5", "tz": "Europe/Berlin", "limits": {"max_cost_usd": 2}, "archive_after": "14d"}]
  ```
  Keys: `name`, `prompt`, one of `cron` / `every` (`"6h"`) / `at` (`"2026-11-01T09:00"`) or a `trigger` object, and optionally `id`, `agent`, `tz`, `enabled`, `overlap` (`skip` / `queue` / `parallel`), `missed` (`run_once` / `skip`), `grace`, `limits` (`max_turns`, `max_tokens`, `max_cost_usd`, `honor_no_limits`), `unattended`, `max_failures`, `end` (`after_runs`, `until`), `archive_after`.
- Only the `serve` process runs schedules, and only one process per data dir (it holds `artifacts/scheduler.lock`; a second one says so and does not run them). In Docker, schedules are kept on the `/data` volume. Each HQ runs its own schedules; the office relay can read `schedules` of the HQs it lists. An external cron or systemd timer calling `POST /api/tasks` still works if you prefer it.
