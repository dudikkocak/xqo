from __future__ import annotations

import asyncio
import contextlib
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .store import EventStore, JsonlStore
from .attention import AttentionCenter
from .departments import HQ
from .chat import Chat
from .limits import Limits
from .models import Event, Task, short_title, task_stamp

if TYPE_CHECKING:
    from .agent import Agent

MAX_DEPTH = 4
OFFER_WINDOW = 1.5  # seconds to collect offers after a broadcast
TASK_TIMEOUT = float(os.getenv("XQO_TASK_TIMEOUT", "900"))  # per (sub)task, seconds
HISTORY_LIMIT = 5000  # events kept in memory / replayed on restart
FORK_CONTEXT_CHARS = 20_000  # transcript handed to a forked task
SKIP_IN_TRANSCRIPT = {"llm"}
ACTIVE = {"pending", "running", "needs_attention"}  # statuses that block deletion
CANCEL_GRACE = 10.0  # seconds a cancelled task gets to clean up (kill processes, close requests)
TOMBSTONES = {"task_deleted", "task_restored"}  # kept in the log when a task is purged
# Template slots an LLM sometimes leaves in a message instead of the real text: "{content}" or "<code>"
# alone on a line, "{code}"-style slots in prose, "[insert the code here]".
_SLOT_NAMES = (r"content|contents|code|source|source_code|file|file_content|file_contents|text|input|data|diff|"
               r"document|snippet|output|result|spec|requirements")
PLACEHOLDER_RES = [
    re.compile(r"^\s*(\{\{?\s*[A-Za-z_][\w.]*\s*\}\}?|\$\{[A-Za-z_]\w*\}|<(?:" + _SLOT_NAMES + r")>"
               r"|<[A-Za-z_]+(?: [A-Za-z_]+)+>)\s*$", re.M | re.I),
    re.compile(r"\{\{?\s*(?:" + _SLOT_NAMES + r")\s*\}\}?", re.I),
    re.compile(r"[\[<]\s*(?:insert|paste|add|put)\b[^\]>\n]{0,60}\bhere\s*[\]>]", re.I),
]
FENCE_RE = re.compile(r"^\s*(```|~~~).*?^\s*\1", re.S | re.M)


def find_placeholders(text: str) -> list[str]:
    """Unfilled template slots in a message (code fences are ignored, they may hold real braces)."""
    prose = FENCE_RE.sub("", text or "")
    return list(dict.fromkeys(m.group(0).strip() for r in PLACEHOLDER_RES for m in r.finditer(prose)))


MAX_TEST_FAILS = 3  # failed test runs in a row before the user is asked what to do


class Draining(RuntimeError):
    """New work is refused while the server drains before an upgrade."""


class HandoverError(RuntimeError):
    pass


class MessageBus:
    """Routes direct messages, skill broadcasts and handovers; fans every event out to monitors."""

    def __init__(self, log_path: str | Path = "logs/events.jsonl", store: EventStore | None = None):
        self.agents: dict[str, Agent] = {}
        self.tasks: dict[str, Task] = {}
        self.history: list[dict] = []
        self._subscribers: set[asyncio.Queue] = set()
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.store: EventStore = store or JsonlStore(self.log_path)  # the event log (store.py): jsonl
        self.titler = None  # async (prompt) -> one-line title; None = heuristic
        self.offer_judge = None  # OfferJudge (offer_judge.py): a model breaks skill ties / judges large tasks
        self._reserved: set[str] = set()
        self.departments: dict = {}  # name -> Department (config.build fills this)
        self.budget = None
        self.attention = AttentionCenter(self)
        self._jobs: dict[str, asyncio.Task] = {}  # root task id -> the asyncio task running it
        self._cancels: dict[str, dict] = {}  # root task id -> {by, reason} while a cancel is in progress
        self._background: set[asyncio.Task] = set()
        self.lifecycle = None  # TaskLifecycle (lifecycle.py): archive and trash, set by config.build
        self.plugins = None  # PluginManager (plugin.py), set by config.build
        # services of active plugins: the plugin manager sets bus.<Plugin.service> (None when the plugin is off).
        # Other plugins and the core look them up with PluginContext.get / bus.plugins.service(name).
        self.office = None  # office plugin (OfficeConfig), built in
        self.chat = Chat(self)
        self.limits = Limits()  # system-wide "no limits" hour (limits.py); build() persists it
        self.usage_total = None  # UsageTotals (usage.py), set by config.build: the office header's overall total
        self.office_edit = False  # office layout editing: only with `serve --edit` (office plugin)
        self.hq_name = ""  # "name" in agents.json: shown in the dashboard and office headers
        self.draining = False  # before an upgrade: no new tasks, and plugins' background work waits (on_hold)
        self.quiesced = 0  # while a backup is written: plugins' background work waits (on_hold)
        self._restore()

    def on_hold(self) -> bool:
        """Whether background starters (plugins' schedules, feeds, polling, ...) should wait."""
        return self.draining or self.quiesced > 0

    def running_roots(self) -> list[Task]:
        return [t for t in self.tasks.values() if t.parent_id is None and self.is_running(t.id)]

    def _restore(self) -> None:
        """Reload task records (all of them) and the recent events (HISTORY_LIMIT) from the store after a restart."""
        for d in self.store.seeds():  # every task, however old: the task list doesn't lose old tasks
            self._ingest(d)
        for d in self.store.recent(HISTORY_LIMIT):  # the dashboard's live history, attention and alerts
            self.history.append(d)
            if d["kind"].startswith("attention_"):
                self.attention.restore(d)
            self.attention.observe(d)

    def _ingest(self, d: dict) -> None:
        """Rebuild task records (and attention items) from one logged event."""
        if d["kind"].startswith("attention_"):
            self.attention.restore(d)
        tid = d.get("task_id")
        if d["kind"] in ("task", "direct", "handover") and tid:
            self.tasks[tid] = Task(prompt=d["content"], assignee=d["recipient"], requester=d["sender"],
                                   parent_id=d.get("parent_task_id"), id=tid, created=d["ts"],
                                   root_id=d.get("root_task_id") or tid, status="interrupted",
                                   title=d.get("meta", {}).get("title", ""),
                                   forked_from=d.get("meta", {}).get("forked_from"),
                                   attachments=d.get("meta", {}).get("attachments") or [],
                                   schedule=d.get("meta", {}).get("schedule"),
                                   limits=d.get("meta", {}).get("limits") or {},
                                   unattended=bool(d.get("meta", {}).get("unattended")))
        elif d["kind"] == "user_message" and d.get("meta", {}).get("attachments"):
            root = self.tasks.get(d.get("root_task_id") or tid)  # files sent with an Ask / Steer message
            if root:
                known = {a["name"] for a in root.attachments}
                root.attachments = root.attachments + [a for a in d["meta"]["attachments"] if a["name"] not in known]
        elif d["kind"] == "done" and tid in self.tasks:
            t = self.tasks[tid]
            t.status, t.result = d.get("meta", {}).get("status", "done"), d["content"]
            t.tests = d.get("meta", {}).get("tests") or t.tests
            t.usage = d.get("meta", {}).get("usage") or t.usage
        elif d["kind"] == "task_paused" and tid in self.tasks:
            self.tasks[tid].status = "paused"  # paused when the server stopped: Resume reopens it
        elif d["kind"] in ("task_resumed", "task_reopened") and tid in self.tasks:
            self.tasks[tid].status = "interrupted"  # until its next done event

    def spawn(self, coro) -> asyncio.Task:
        """Run a coroutine in the background, keeping a reference until it ends."""
        job = asyncio.ensure_future(coro)
        self._background.add(job)
        job.add_done_callback(self._background.discard)
        return job

    # ---- hiding / reloading task trees (used by the trash) --------------
    def tree_ids(self, root_id: str) -> set[str]:
        return {t.id for t in self.tasks.values() if t.root_id == root_id} | {root_id}

    def hide(self, root_id: str, ids: set[str] | None = None) -> set[str]:
        """Drop a task tree from memory (tasks and in-memory history); the log file is untouched."""
        ids = ids or self.tree_ids(root_id)
        for tid in ids:
            self.tasks.pop(tid, None)
        self.history = [e for e in self.history if e["kind"] in TOMBSTONES
                        or (e.get("root_task_id") != root_id and e.get("task_id") not in ids)]
        return ids

    def reload(self, root_id: str) -> list[dict]:
        """Bring a hidden task tree back from the full log (undo of a delete)."""
        events = self.task_events(root_id)
        for d in events:
            self._ingest(d)
        self.history = sorted(self.history + events, key=lambda e: e["ts"])[-HISTORY_LIMIT:]
        if self.lifecycle:  # restored from the trash: still archived if it was
            self.lifecycle.apply(root_id)
        return events

    def purge_log(self, root_id: str, ids: set[str]) -> int:
        """Remove a task tree's events from the store (tombstones stay). Returns how many were removed."""
        return self.store.purge(root_id, ids, TOMBSTONES)

    # ---- registry -------------------------------------------------------
    def register(self, agent: Agent) -> None:
        self.agents[agent.name] = agent
        agent.bus = self

    def agent_id(self, ref: str | None) -> str | None:
        """The agent id for what a person typed: the id itself, else a nickname (case-insensitive, unique at
        startup). None when nothing matches. Agent-to-agent tools take ids only."""
        if not ref:
            return None
        if ref in self.agents:
            return ref
        r = ref.strip().lstrip("@").casefold()
        if r in self.agents:
            return r
        return next((a.name for a in self.agents.values() if a.nickname and a.nickname.casefold() == r), None)

    def roster(self) -> list[dict]:
        return [a.describe() for a in self.agents.values()]

    def dept_of(self, agent: str | None) -> str | None:
        a = self.agents.get(agent or "")
        return getattr(a, "department", HQ) if a else None

    def _may_contact(self, src: str, dst: str) -> bool:
        if src == dst or HQ in (src, dst):
            return True
        d = self.departments.get(src)
        return d.may_contact(dst) if d else True

    # ---- monitoring -----------------------------------------------------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def emit(self, event: Event) -> Event:
        d = event.to_dict()
        d["dept"] = self.dept_of(event.sender)
        d["dept_to"] = self.dept_of(event.recipient)
        t = self.tasks.get(event.task_id or "")
        d["root_task_id"] = t.root_id if t else event.task_id
        if t:
            d["task_name"] = t.name
            root = self.tasks.get(t.root_id)
            d["root_task_name"] = root.name if root else t.name
        self.history.append(d)
        self.attention.observe(d)
        if len(self.history) > HISTORY_LIMIT:
            del self.history[: len(self.history) - HISTORY_LIMIT]
        self.store.append(d)
        for q in list(self._subscribers):
            if q.full():  # slow monitor: drop its oldest event rather than block agents
                q.get_nowait()
            q.put_nowait(d)
        return event

    # ---- tasks ----------------------------------------------------------
    def _new_id(self) -> str:
        """task_<YYMMDDhhmm>, with -2, -3 ... when several tasks start in the same minute."""
        base = task_stamp()
        tid, n = base, 1
        store = getattr(self, "artifacts", None)
        while tid in self.tasks or tid in self._reserved or (store and ((store.root / tid).exists()
                                                                     or (store.root / ".trash" / tid).exists())):
            n += 1
            tid = f"{base}-{n}"
        self._reserved.add(tid)
        return tid

    async def _title(self, prompt: str) -> str:
        if self.titler:
            try:
                t = short_title(await self.titler(prompt), 80)
                if t and t != "untitled":
                    return t
            except Exception:  # titles are cosmetic; never fail a task over one
                pass
        return short_title(prompt)

    def acknowledge(self, tid: str, name: str) -> None:
        """An instant "got it" from the agent a new task goes to, before its title and first model call."""
        sem = getattr(self.agents.get(name), "_sem", None)
        busy = getattr(self.agents.get(name), "active", 0) if sem is not None and sem.locked() else 0
        self.emit(Event("ack", name, "user", tid, content=f"Got it. I'm finishing {busy} other task{'s' if busy != 1 else ''} first, then I'm on it."
                        if busy else "Got it, I'm on it.", meta={"queued": busy}))

    async def create(self, prompt: str, agent: str | None = None, context: str = "",
                     forked_from: dict | None = None, files: list[dict] | None = None,
                     copy_attachments_from: str | None = None, title: str | None = None,
                     meta: dict | None = None) -> Task:
        """Register a new root task (not started yet). Picks the given agent or the first one.
        `meta` (scheduled runs): {"schedule": {...}, "limits": {...}, "unattended": bool}, kept in the task event."""
        if self.draining:
            raise Draining("the server is draining before a restart: new tasks start after it")
        name = agent or next(iter(self.agents))
        name = self.agent_id(name) or name  # a person may give the nickname
        if name not in self.agents:
            raise KeyError(f"unknown agent {name}")
        tid = self._new_id()
        extra = {k: v for k, v in (meta or {}).items() if k in ("schedule", "limits", "unattended") and v}
        if not extra.get("schedule"):
            self.acknowledge(tid, name)
        task = Task(prompt=prompt, assignee=name, chain=[name], id=tid,
                    title=title or await self._title(prompt or "Attached files: " + ", ".join(f["name"] for f in files or [])),
                    context=context, forked_from=forked_from, schedule=extra.get("schedule"),
                    limits=extra.get("limits") or {}, unattended=bool(extra.get("unattended")))
        if copy_attachments_from and copy_attachments_from in self.tasks:
            self.attachments.copy(copy_attachments_from, tid)
            task.attachments = list(self.tasks[copy_attachments_from].attachments)
        if files:  # validated by attachments.validate
            task.attachments += self.attachments.save(tid, files)
        self.tasks[task.id] = task
        meta = {"title": task.title, **({"forked_from": forked_from} if forked_from else {}),
                **({"attachments": task.attachments} if task.attachments else {}), **extra}
        self.emit(Event("task", "user", name, task.id, content=prompt, meta=meta))
        self.artifacts.write_meta(task.id, {"id": task.id, "title": task.title, "name": task.name,
                                            "prompt": prompt, "created": task.created, **meta})
        return task

    async def submit(self, prompt: str, agent: str | None = None, files: list[dict] | None = None) -> Task:
        """Entry point for user tasks: create and run to completion."""
        return await self._run(await self.create(prompt, agent, files=files))

    # ---- replay & fork --------------------------------------------------
    def task_events(self, root_id: str) -> list[dict]:
        """Every logged event of one task tree, oldest first (read from the store, not the memory cap)."""
        return [d for d in self.store.tree(root_id) if d["kind"] not in TOMBSTONES and d["kind"] != "ack"]

    @staticmethod
    def transcript(events: list[dict], limit: int = FORK_CONTEXT_CHARS) -> str:
        lines = []
        for e in events:
            if e["kind"] in SKIP_IN_TRANSCRIPT:
                continue
            to = f" -> {e['recipient']}" if e.get("recipient") else ""
            body = (e.get("content") or "").strip()
            if len(body) > 1200:
                body = body[:1200] + " …"
            lines.append(f"[{e['ts'][11:19]}] {e['kind']} {e['sender']}{to} ({e.get('task_id')}): {body}")
        text = "\n".join(lines)
        if len(text) > limit and lines:  # keep the original request and the most recent events
            head, tail, n = lines[0], [], 0
            for l in reversed(lines[1:]):
                if n + len(l) > limit - len(head) - 80:
                    break
                tail.append(l)
                n += len(l) + 1
            text = "\n".join([head, f"… ({len(lines) - 1 - len(tail)} events omitted) …", *reversed(tail)])
        return text

    async def fork(self, root_id: str, event_id: str, prompt: str | None = None,
                   agent: str | None = None) -> Task:
        """Branch a past task at one of its events: new root task, same workspace/notes as at that point,
        the earlier message flow as context, and an optionally edited prompt or entry agent."""
        events = self.task_events(root_id)
        idx = next((i for i, e in enumerate(events) if e["id"] == event_id), None)
        if idx is None:
            raise KeyError(f"event {event_id} not found in {root_id}")
        first = next((e for e in events if e["kind"] == "task" and e["task_id"] == root_id), events[0])
        cut, upto = events[idx], events[: idx + 1]
        src = self.tasks.get(root_id)
        src_name = src.name if src else first.get("task_name", root_id)
        # rebuild state from the events themselves: exact even when several files share a timestamp
        files = self.artifacts.state_from([e["meta"] for e in upto if e["kind"] == "artifact"])
        notes = [e["meta"] for e in upto if e["kind"] == "memory" and e["meta"].get("key")]
        origin = {"task_id": root_id, "task_name": src_name, "event_id": event_id,
                  "event_kind": cut["kind"], "event_ts": cut["ts"]}
        context = (f"This task is a fork of an earlier run ({src_name}), branched at its {cut['kind']} event "
                   f"from {cut['sender']} at {cut['ts'][11:19]}. The workspace files and notes board were "
                   f"restored to that point. The earlier run's message flow up to there:\n\n"
                   + self.transcript(upto))
        task = await self.create(prompt or first["content"], agent or first.get("recipient"),
                                 context=context, forked_from=origin, copy_attachments_from=root_id)
        recs = self.artifacts.restore(task.id, files, "user", f"fork:{root_id}@{event_id}",
                                      f"fork of {root_id} at {cut['kind']} {cut['ts']}")
        self.memory.restore(task.id, notes)
        self.emit(Event("fork", "user", task.assignee, task.id, None,
                        f"Forked from {src_name} at {cut['kind']} ({cut['ts'][11:19]}): "
                        f"{len(files)} file(s) and {len(self.memory.notes(task.id))} note(s) restored",
                        {**origin, "files": sorted(files), "commit": recs[0].get("commit") if recs else None}))
        return task

    def waiting_on_user(self, task_id: str) -> bool:
        """True if this task or any of its sub-tasks has a pending attention request."""
        for a in self.attention.pending():
            t = self.tasks.get(a.task_id)
            while t:
                if t.id == task_id:
                    return True
                t = self.tasks.get(t.parent_id or "")
        return False

    def limits_lifted(self, task: Task) -> bool:
        """The system-wide "no limits" override (limits.py) is on and applies to this task's tree."""
        lim = getattr(self, "limits", None)
        if not lim:
            return False
        root = self.tasks.get(task.root_id) if task.root_id else None
        return lim.lifts(getattr(root or task, "limits", None))

    def lift_pending(self) -> list[str]:
        """"No limits" was turned on: answer open budget / time-limit questions with 'extend'."""
        done = []
        for a in list(self.attention.pending()):
            t = self.tasks.get(a.task_id)
            if a.kind in ("budget", "timeout") and t and self.limits_lifted(t):
                with contextlib.suppress(KeyError, ValueError):
                    self.attention.resolve(a.id, "extend", by="no_limits")
                    done.append(a.id)
        return done

    async def _supervise(self, task: Task):
        """Run the agent with a time limit that pauses while the user is being asked something.

        On expiry the user is flagged: 'extend' grants another period, anything else stops the task."""
        job = asyncio.create_task(self.agents[task.assignee].handle(task))
        limit, last = TASK_TIMEOUT, time.monotonic()
        try:
            while True:
                left = limit - ((time.monotonic() - task.created_mono) - task.paused)
                done, _ = await asyncio.wait({job}, timeout=max(0.01, min(0.25, left)))
                now = time.monotonic()
                if self.waiting_on_user(task.id) or self.chat.is_paused(task.root_id):
                    task.paused += now - last  # time on hold doesn't count against the limit
                last = now
                if job in done:
                    return job.result()
                active = (now - task.created_mono) - task.paused
                if self.limits_lifted(task):  # "no limits": a fresh period counts from when the override ends
                    limit = max(limit, active + TASK_TIMEOUT)
                    continue
                if active < limit:
                    continue
                t0 = time.monotonic()
                answer, _ = await self.attention.request(
                    task, task.assignee, "timeout",
                    f"{task.name} has run for {int(active)}s (limit {int(limit)}s). Extend it or stop it?",
                    default="stop", options=["extend", "stop"])
                task.paused += time.monotonic() - t0
                last = time.monotonic()
                if job.done():
                    return job.result()
                if answer.strip().lower() not in ("extend", "continue", "yes", "y"):
                    raise asyncio.TimeoutError(f"stopped after {int(active)}s time limit")
                limit += TASK_TIMEOUT
        finally:
            if not job.done():
                job.cancel()
                with contextlib.suppress(BaseException):  # let it kill processes and close requests
                    await asyncio.wait({job}, timeout=CANCEL_GRACE)

    def save_report(self, task: Task) -> dict | None:
        """Save the agent's final answer as a Markdown report (artifact history, listed in the office).

        A failed or cancelled task gets a short report: what happened and the files it touched."""
        store = getattr(self, "artifacts", None)
        if not store or task.assignee not in self.agents:
            return None
        result = str(task.result or "").strip()
        if task.status == "done" and not result:
            return None
        title = short_title(task.title or task.prompt or task.id)
        head = f"# {title}\n\n**Agent:** {task.assignee} · **Status:** {task.status} · **Task:** `{task.id}`\n\n"
        if task.status == "done":
            body = head + result + "\n"
        else:
            try:
                files = sorted({r["file"] for r in store.list(task.id) if r.get("file") and r["task_id"] == task.id})
            except (OSError, ValueError):
                files = []
            body = (head + f"## What happened\n\n{result or task.status}\n\n## Files touched\n\n"
                    + ("\n".join(f"- `{f}`" for f in files) or "_none_") + "\n")
        try:
            return store.save_report(task.root_id or task.id, task.id, task.assignee, title, body,
                                     status=task.status, auto=True)
        except OSError:
            return None

    async def _run(self, task: Task) -> Task:
        self.tasks[task.id] = task
        task.status = "running"
        task.created_mono = time.monotonic()
        is_root = task.id == task.root_id
        if is_root:
            self._jobs[task.id] = asyncio.current_task()
        try:
            task.result = await self._supervise(task)
            task.status = "done"
            self.save_report(task)
        except asyncio.CancelledError:
            info = self._cancels.get(task.root_id)
            task.status = "cancelled"
            task.result = f"Cancelled by {info['by']}" + (f": {info['reason']}" if info and info.get("reason") else "") \
                if info else "Cancelled"
            self.save_report(task)
            self.emit(Event("done", task.assignee, task.requester, task.id, task.parent_id,
                            task.result, {"status": task.status, **({"usage": task.usage} if task.usage else {})}))
            if not (is_root and info):
                raise  # a sub-task: let the cancellation reach its parent
            current = asyncio.current_task()
            if current and hasattr(current, "uncancel"):
                current.uncancel()
            self._finish_cancel(task, info)
            if self.plugins:
                await self.plugins.task_done(task)
            return task
        except Exception as e:  # surface failures to UI and caller
            task.status, task.result = "error", f"{type(e).__name__}: {e or 'timeout'}"
            self.emit(Event("error", task.assignee, task.requester, task.id, task.parent_id, task.result))
            self.save_report(task)
        finally:
            if is_root:
                self._jobs.pop(task.id, None)
                self._cancels.pop(task.id, None)
                self.chat.clear(task.id)
        self.emit(Event("done", task.assignee, task.requester, task.id, task.parent_id,
                        task.result or "", {"status": task.status, **({"tests": task.tests} if task.tests else {}),
                                            **({"usage": task.usage} if task.usage else {})}))
        if is_root and self.plugins:  # e.g. a task started by a peer: the peering plugin sends the result back
            await self.plugins.task_done(task)
        return task

    # ---- usage ----------------------------------------------------------
    def record_usage(self, task: Task, agent: str, session: dict) -> dict:
        """Add a Claude Code session's usage and cost (from its result messages) to the task."""
        def add(into: dict, more: dict) -> dict:
            out = dict(into)
            for k, v in more.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    out[k] = round(out.get(k, 0) + v, 6)
            return out

        u = add(task.usage or {}, session)
        u["sessions"] = u.get("sessions", 0) + 1
        models = dict(u.get("models") or {})
        for m, mu in (session.get("models") or {}).items():
            models[m] = add(models.get(m, {}), mu)
        if models:
            u["models"] = models
        task.usage = u
        if self.usage_total:
            self.usage_total.add(session)
        tokens = sum(session.get(k, 0) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                                  "cache_creation_input_tokens"))
        self.emit(Event("llm", agent, None, task.id, task.parent_id,
                        f"session usage: ${session.get('cost_usd', 0):.4f}, {tokens:,} tokens, "
                        f"{session.get('turns', 0)} turns", {"backend": "claude-sdk", "session_usage": session,
                                                              "task_usage": u}))
        return u

    def record_rate_limit(self, agent: str, task_id: str | None, info) -> dict | None:
        """A Claude Code plan limit update (RateLimitEvent): kept for the header chip, and told to the dashboard."""
        if not self.usage_total:
            return None
        e = self.usage_total.rate_limit(info)
        if e:
            pct = "" if e["utilization"] is None else f" {round(e['utilization'] * 100)}%"
            self.emit(Event("llm", agent, None, task_id, None,
                            f"plan limit {e['type'].replace('_', ' ')}{pct}: {e['status'].replace('_', ' ')}",
                            {"backend": "claude-sdk", "rate_limit": e}))
        return e

    # ---- test results ---------------------------------------------------
    def record_test(self, task: Task, exit_code: int | None) -> dict:
        """Count a run_code result on the task and every ancestor (the root ends with the tree's totals)."""
        failed = exit_code != 0
        t = task
        while t:
            s = t.tests or {"runs": 0, "failed": 0, "fails_in_row": 0}
            s["runs"] += 1
            s["failed"] += failed
            s["fails_in_row"] = s.get("fails_in_row", 0) + 1 if failed else 0
            s["last_exit"] = exit_code
            t.tests = s
            t = self.tasks.get(t.parent_id or "")
        return task.tests

    @staticmethod
    def test_note(child: Task) -> str:
        """Appended to a sub-task's result when its last test run failed, so the requester can't miss it."""
        s = child.tests
        if not s or not s.get("runs") or s.get("last_exit") == 0:
            return ""
        return (f"\n\n[xqo: the last test run in this sub-task FAILED (exit {s.get('last_exit')}; "
                f"{s['failed']} of {s['runs']} runs failed). Get it fixed (e.g. hand the failures back to the "
                "author and re-test) or report it clearly to the user.]")

    # ---- cancel ---------------------------------------------------------
    def is_running(self, root_id: str) -> bool:
        job = self._jobs.get(root_id)
        return bool(job and not job.done())

    async def cancel(self, root_id: str, by: str = "user", reason: str = "", wait: float = CANCEL_GRACE) -> Task:
        """Stop a running task tree: model calls are dropped, open requests closed, processes killed.
        Artifacts made so far are kept and a final commit records the workspace state."""
        root = self.tasks.get(root_id)
        if root is None or root.root_id != root_id:
            raise KeyError(f"unknown task {root_id}")
        job = self._jobs.get(root_id)
        if not job or job.done():
            raise ValueError(f"{root.name} is not running (status: {root.status})")
        if root_id not in self._cancels:
            self._cancels[root_id] = {"by": by, "reason": reason.strip()[:500]}
            self.emit(Event("task_cancel_requested", by, root.assignee, root_id, None,
                            f"Cancel requested by {by}" + (f": {reason.strip()}" if reason.strip() else ""),
                            {"by": by, "reason": reason.strip()[:500]}))
            job.cancel()
        if wait:
            await asyncio.wait({job}, timeout=wait)
        return root

    def _finish_cancel(self, root: Task, info: dict) -> None:
        stopped = sorted(t.id for t in self.tasks.values() if t.root_id == root.id and t.status == "cancelled")
        commit = self.artifacts.final_commit(root.id, f"Task cancelled by {info['by']}"
                                             + (f": {info['reason']}" if info.get("reason") else ""), info["by"])
        extra = self.on_cancelled(root, info) or {}
        self.emit(Event("task_cancelled", info["by"], None, root.id, None, root.result or "Cancelled",
                        {"by": info["by"], "reason": info.get("reason", ""), "stopped": stopped,
                         "commit": commit, **extra}))

    def on_cancelled(self, root: Task, info: dict) -> dict | None:
        """Hook for extensions (departments) to settle their own state after a cancel; returns extra event meta.

        Every department's sub-tasks were cancelled with the tree; report the usage counted up to now."""
        budget = getattr(self, "budget", None)
        if not budget:
            return None
        return {"usage": {d: {"tokens": u["tokens"], "cost": round(u["cost"], 4)}
                          for (r, d), u in budget.used.items() if r == root.id}}

    def on_purged(self, root_id: str) -> None:
        """Hook for extensions to drop in-memory state of a permanently deleted task."""
        if self.lifecycle:
            self.lifecycle.forget(root_id)
        budget = getattr(self, "budget", None)
        if budget:  # per-task usage goes; department totals (overall spend) stay
            for key in [k for k in budget.used if k[0] == root_id]:
                budget.used.pop(key, None)
                budget.extensions.pop(key, None)
                budget._locks.pop(key, None)
                budget.offsets.pop(key, None)
                budget.lifted.discard(key)

    def tree(self, task_id: str) -> dict:
        t = self.tasks[task_id]
        kids = [c for c in self.tasks.values() if c.parent_id == task_id]
        return {**t.to_dict(), "children": [self.tree(c.id) for c in kids]}

    def _child(self, parent: Task, assignee: str, prompt: str, title: str = "",
               attachments: list[str] | None = None) -> Task:
        if parent.depth + 1 > MAX_DEPTH:
            raise HandoverError(f"max delegation depth {MAX_DEPTH} reached")
        picked = self._pick_attachments(parent, attachments)
        return Task(prompt=prompt, assignee=assignee, requester=parent.assignee, parent_id=parent.id,
                    chain=parent.chain + [assignee], depth=parent.depth + 1, root_id=parent.root_id,
                    id=self._new_id(), title=short_title(title, 80) if title else "",
                    attachments=picked)

    def _pick_attachments(self, parent: Task, names: list[str] | None) -> list[dict]:
        """Resolve attachment names (forwarded by an agent) against the root task's attachments."""
        root = self.tasks.get(parent.root_id)
        known = {a["name"]: a for a in (root.attachments if root else [])}
        missing = [n for n in names or [] if n not in known]
        if missing:
            raise HandoverError(f"unknown attachment(s) {missing}. Available: {', '.join(known) or 'none'}")
        return [known[n] for n in dict.fromkeys(names or [])]

    def _compose(self, parent: Task, text: str, files: list[str] | None) -> str:
        """Check a delegated message for unfilled placeholders and append the referenced workspace files as paths."""
        slots = find_placeholders(text)
        if slots:
            raise HandoverError(
                f"message contains unfilled placeholder(s) {slots}; the receiver would get them literally. "
                "Put the real content in the message (code inside a fenced block), or name workspace files in "
                "the 'files' field.")
        if not files:
            return text
        store = getattr(self, "artifacts", None)
        if store is None:
            raise HandoverError("no workspace: files cannot be forwarded")
        parts, missing = [], []
        for path in dict.fromkeys(files):
            try:
                content = store.read_file(parent.root_id, path)
            except Exception:  # noqa: BLE001 - missing or invalid path
                missing.append(path)
                continue
            parts.append(f"- {path} ({len(content):,} chars)")
        if missing:
            known = [f["file"] for f in store.list_files(parent.root_id)]
            raise HandoverError(f"unknown workspace file(s) {missing}. Available: {', '.join(known) or 'none'}")
        return (text + "\n\nWorkspace files to read (not included here: open them with Read or read_file when you "
                       "need them):\n" + "\n".join(parts))

    # ---- direct ---------------------------------------------------------
    async def direct(self, parent: Task, to: str, message: str, title: str = "",
                     attachments: list[str] | None = None, files: list[str] | None = None) -> str:
        if to not in self.agents:
            raise HandoverError(f"unknown agent '{to}'. Known: {', '.join(self.agents)}")
        if to in parent.chain:
            raise HandoverError(f"'{to}' is already working on this task chain {parent.chain}")
        src, dst = self.dept_of(parent.assignee), self.dept_of(to)
        if src not in (dst, HQ) and dst != HQ:  # headquarters may talk to anyone
            if not self._may_contact(src, dst):
                raise HandoverError(f"department '{src}' may not contact department '{dst}'")
            head = self.departments[dst].head if dst in self.departments else None
            if to != head:
                raise HandoverError(f"'{to}' is in the {dst} department; send work for it to its head '{head}'")
        child = self._child(parent, to, self._compose(parent, message, files), title, attachments)
        self.tasks[child.id] = child
        self.emit(Event("direct", parent.assignee, to, child.id, parent.id, message,
                        {"title": child.title, **({"attachments": child.attachments} if child.attachments else {}),
                         **({"files": list(dict.fromkeys(files))} if files else {})}))
        done = await self._run(child)
        self.emit(Event("reply", to, parent.assignee, child.id, parent.id, done.result or "",
                        {"status": done.status, **({"tests": done.tests} if done.tests else {})}))
        return (done.result or "") + self.test_note(done)

    # ---- broadcast skill lookup + handover ------------------------------
    @staticmethod
    def split_target(skill: str, department: str | None = None) -> tuple[str, str | None]:
        """'@marketing copywriting' -> ('copywriting', 'marketing')."""
        s = skill.strip()
        if s.startswith("@"):
            head, _, rest = s[1:].partition(" ")
            department, s = department or head.strip().lower(), rest.strip() or s
        return s, (department.strip().lower() if department else None)

    async def _collect(self, parent: Task, candidates: list, skill: str, task_text: str) -> list[dict]:
        jobs = [asyncio.create_task(c(skill, task_text)) for c in candidates]
        if not jobs:
            return []
        try:
            done, pending = await asyncio.wait(jobs, timeout=OFFER_WINDOW)
        except asyncio.CancelledError:
            for j in jobs:
                j.cancel()
            raise
        for j in pending:
            j.cancel()
        return [j.result() for j in jobs if j in done and not j.exception() and j.result()]

    async def _head_offer(self, dept, skill: str, task_text: str) -> dict | None:
        """A head offers on behalf of its department when any member has the skill."""
        members = [self.agents[n] for n in dept.members if n in self.agents]
        offers = [o for o in [await m.consider_offer(skill, task_text) for m in members] if o]
        if not offers:
            return None
        best = max(offers, key=lambda o: o["score"])
        return {"agent": dept.head, "skill": skill, "match": best["match"], "score": best["score"],
                "load": sum(m.active for m in members), "department": dept.name, "via": "head",
                "member": best["agent"]}

    async def broadcast_skill(self, parent: Task, skill: str, task_text: str,
                              department: str | None = None) -> list[dict]:
        """Two-level lookup: the sender's own department first, then the heads of the departments it may
        contact. `department` (or '@dept skill') sends the lookup straight to one department."""
        sender = parent.assignee
        skill, target = self.split_target(skill, department)
        src = self.dept_of(sender) or HQ
        if target and target != HQ and target not in self.departments:
            raise HandoverError(f"unknown department '{target}'. Known: {', '.join(self.departments) or 'none'}")
        if target and not self._may_contact(src, target):
            raise HandoverError(f"department '{src}' may not contact department '{target}'")

        def members(dept: str):
            return [a.consider_offer for a in self.agents.values()
                    if getattr(a, "department", HQ) == dept and a.name not in parent.chain]

        def heads(only: str | None = None):
            out = []
            for d in self.departments.values():
                if d.name == src or (only and d.name != only) or not self._may_contact(src, d.name):
                    continue
                if d.head in parent.chain or d.head not in self.agents:
                    continue
                out.append(lambda s, t, d=d: self._head_offer(d, s, t))
            return out

        if target and target != src:
            levels = [("department", target, members(HQ) if target == HQ else heads(target))]
        else:
            levels = [("department", src, members(src))]
            if not target and self.departments:
                levels.append(("heads", None, heads()))
        offers = []
        for scope, dept, candidates in levels:
            where = f"department '{dept}'" if scope == "department" else "department heads"
            self.emit(Event("broadcast", sender, "*", parent.id, parent.parent_id,
                            f"Looking for skill '{skill}'" + (f" in {where}" if self.departments else "")
                            + f": {task_text}", {"skill": skill, "scope": scope, "department": dept}))
            offers = await self._collect(parent, candidates, skill, task_text)
            if offers:
                break
        for o in offers:
            via = f" for {o['department']} ({o['member']})" if o.get("via") == "head" else ""
            self.emit(Event("offer", o["agent"], sender, parent.id, parent.parent_id,
                            f"I can do '{skill}'{via} (match={o['match']}, load={o['load']})", o))
        return offers

    async def handover(self, parent: Task, skill: str, task_text: str, title: str = "",
                       attachments: list[str] | None = None, department: str | None = None,
                       files: list[str] | None = None) -> str:
        self._pick_attachments(parent, attachments)  # fail fast, before broadcasting
        full_text = self._compose(parent, task_text, files)
        offers = await self.broadcast_skill(parent, skill, task_text, department)
        skill, _ = self.split_target(skill, department)
        if not offers:
            choices = [n for n in self.agents if n not in parent.chain]
            answer, auto = await self.attention.request(
                parent, parent.assignee, "no_offer",
                f"No agent offered skill '{skill}' for: {title or task_text[:200]}. "
                f"Reply with an agent name to assign it to, or 'skip'.",
                default="skip", options=choices + ["skip"])
            pick = answer.strip()
            if pick not in choices:
                raise HandoverError(f"no agent offered skill '{skill}'"
                                    + (" (auto-confirmed: skip)" if auto else f" (user: {answer})"))
            offers = [{"agent": pick, "skill": skill, "match": "user", "score": 3,
                       "load": self.agents[pick].active}]
        ranked = sorted(offers, key=lambda o: (-o["score"], o["load"]))
        if self.offer_judge:  # only on a tie or a large task; otherwise this returns `ranked` untouched
            ranked = await self.offer_judge.rank(self, parent, skill, task_text, offers, ranked)
        errors = []
        for best in ranked:  # fall through to the next offer if the chosen agent fails
            child = self._child(parent, best["agent"], full_text, title, attachments)
            self.tasks[child.id] = child
            self.emit(Event("handover", parent.assignee, best["agent"], child.id, parent.id, task_text,
                            {"skill": skill, "offers": [o["agent"] for o in ranked], "title": child.title,
                             "department": self.dept_of(best["agent"]),
                             **({"attachments": child.attachments} if child.attachments else {}),
                             **({"files": list(dict.fromkeys(files))} if files else {})}))
            done = await self._run(child)
            self.emit(Event("result", best["agent"], parent.assignee, child.id, parent.id, done.result or "",
                            {"status": done.status, "skill": skill, **({"tests": done.tests} if done.tests else {})}))
            if done.status == "done":
                return (done.result or "") + self.test_note(done)
            errors.append(f"{best['agent']}: {done.result}")
        raise HandoverError("all offers failed: " + "; ".join(errors))
