"""Attention: everything that needs the user, in one place.

1. Requests: an agent asks (ask_user, approval, budget, time limit, network, ...) and waits; the default answer is
   auto-confirmed after a timeout.
2. Alerts: what keeps an agent blinking until the user deals with it (merged here from the former alerts.py):
     attention       an open question: resolved by answering it
     auto_confirmed  a question nobody answered in time got its default answer: stays until dismissed
     error           a task or sub-task failed: stays until dismissed
     done            a root task finished: stays until dismissed (sub-tasks and unattended scheduled runs don't)
   Alerts are derived from the event log (bus.emit and the replay on start both feed observe()), so they survive a
   restart and every open tab sees the same set. Dismissing is logged as an "alert_acked" event. Deleting,
   archiving, reopening or resuming a task clears its alerts."""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

from .models import Event, new_id, utcnow

if TYPE_CHECKING:
    from .bus import MessageBus
    from .models import Task

ATTENTION_TIMEOUT = 180.0  # seconds before auto-confirm; XQO_ATTENTION_TIMEOUT or agents.json override

# kind -> why the flag was raised
KINDS = {
    "question": "agent asked for input",
    "approval": "agent asked for approval",
    "missing_info": "agent needs missing information",
    "blocked": "agent is blocked",
    "timeout": "task hit its time limit",
    "retries_exhausted": "API retries ran out",
    "no_offer": "no agent offered on a handover",
    "patch_failed": "patch still fails after retries",
    "budget": "department budget reached",
    "tests_failing": "tests keep failing",
    "output_limit": "reply keeps hitting the output limit",
    "network": "agent asks for network access",
}


ALERT_KINDS = ("attention", "auto_confirmed", "error", "done")
CLEARS = {"task_deleted", "task_archived", "task_reopened", "task_resumed"}


@dataclass
class Alert:
    id: str
    kind: str          # attention | auto_confirmed | error | done
    agent: str
    task_id: str
    root_id: str
    text: str
    ts: str
    att_id: str | None = None  # the attention request behind it (kind attention / auto_confirmed)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Attention:
    task_id: str
    agent: str
    kind: str
    question: str
    default: str               # answer used on auto-confirm
    options: list[str] = field(default_factory=list)
    timeout: float = ATTENTION_TIMEOUT
    id: str = field(default_factory=lambda: new_id("att"))
    created: str = field(default_factory=utcnow)
    deadline: float = 0.0      # epoch seconds
    status: str = "pending"    # pending|answered|auto_confirmed|expired|cancelled
    answer: str | None = None
    resolved_by: str | None = None
    frozen: bool = False       # the task is paused: the auto-confirm timer is stopped

    def to_dict(self) -> dict:
        d = asdict(self)
        d["reason"] = KINDS.get(self.kind, self.kind)
        d["remaining"] = max(0.0, self.deadline - time.time()) if self.status == "pending" else 0.0
        return d


class AttentionCenter:
    def __init__(self, bus: MessageBus, timeout: float | None = None):
        self.bus = bus
        self.timeout = timeout if timeout is not None else float(os.getenv("XQO_ATTENTION_TIMEOUT", ATTENTION_TIMEOUT))
        self.items: dict[str, Attention] = {}
        self._futures: dict[str, asyncio.Future] = {}
        self.inbox: dict[str, Alert] = {}  # alerts: what needs the user, per agent

    def pending(self) -> list[Attention]:
        return [a for a in self.items.values() if a.status == "pending"]

    def pending_for(self, task_id: str) -> bool:
        return any(a.task_id == task_id for a in self.pending())

    async def request(self, task: Task, agent: str, kind: str, question: str, default: str = "",
                      options: list[str] | None = None, timeout: float | None = None) -> tuple[str, bool]:
        """Flag the user and wait. Returns (answer, auto_confirmed).
        A scheduled run marked unattended (scheduler.py) gets the default answer at once."""
        root = self.bus.tasks.get(task.root_id) if task.root_id else None
        unattended = bool(getattr(root or task, "unattended", False))
        if unattended:
            timeout = 0.0
        att = Attention(task.id, agent, kind, question, default or (options or ["ok"])[0], options or [],
                        self.timeout if timeout is None else timeout)
        att.deadline = time.time() + att.timeout
        fut = asyncio.get_running_loop().create_future()
        self.items[att.id] = att
        self._futures[att.id] = fut
        prev, task.status = task.status, "needs_attention"
        self.bus.emit(Event("attention_requested", agent, "user", task.id, task.parent_id, question, att.to_dict()))
        try:
            answer, auto = await self._wait(task, att, fut), False
        except asyncio.TimeoutError:
            answer, auto = att.default, True
            self._finish(att, answer, "auto_confirmed", "unattended" if unattended else "timeout")
        except asyncio.CancelledError:  # the task was cancelled while waiting on the user
            if att.status == "pending":
                self._finish(att, "", "cancelled", "cancel")
            raise
        finally:
            self._futures.pop(att.id, None)
            if task.status == "needs_attention" and not self.pending_for(task.id):
                task.status = prev
        return answer, auto

    async def _wait(self, task: Task, att: Attention, fut: asyncio.Future) -> str:
        """Wait for an answer until the deadline; the deadline moves while the task is paused."""
        chat = getattr(self.bus, "chat", None)
        while True:
            left = att.deadline - time.time()
            if left <= 0:
                raise asyncio.TimeoutError
            t0 = time.time()
            done, _ = await asyncio.wait({fut}, timeout=min(left, 0.5))
            if fut in done:
                return fut.result()
            paused = bool(chat and chat.is_paused(task.root_id))
            if paused:
                att.deadline += time.time() - t0
            att.frozen = paused

    def resolve(self, att_id: str, answer: str, by: str = "user") -> Attention:
        att = self.items.get(att_id)
        if not att:
            raise KeyError(att_id)
        if att.status != "pending":
            raise ValueError(f"attention {att_id} is already {att.status}")
        self._finish(att, answer, "answered", by)
        fut = self._futures.get(att_id)
        if fut and not fut.done():
            fut.set_result(answer)
        return att

    def _finish(self, att: Attention, answer: str, status: str, by: str) -> None:
        att.status, att.answer, att.resolved_by = status, answer, by
        self.bus.emit(Event("attention_resolved", by, att.agent, att.task_id, None,
                            f"{status}: {answer}", att.to_dict()))

    def restore(self, d: dict) -> None:
        """Rebuild from logged events; requests still pending at shutdown become 'expired'."""
        meta = d.get("meta", {})
        if d["kind"] == "attention_requested" and meta.get("id"):
            fields = {k: meta[k] for k in ("task_id", "agent", "kind", "question", "default", "options",
                                            "timeout", "id", "created") if k in meta}
            self.items[meta["id"]] = Attention(**fields, status="expired")
        elif d["kind"] == "attention_resolved" and meta.get("id") in self.items:
            a = self.items[meta["id"]]
            a.status, a.answer, a.resolved_by = meta.get("status"), meta.get("answer"), meta.get("resolved_by")

    # ---- alerts: derived from events --------------------------------------------
    def observe(self, d: dict) -> None:
        k, meta = d.get("kind"), d.get("meta") or {}
        tid = d.get("task_id") or ""
        root = d.get("root_task_id") or tid
        if k == "attention_requested" and meta.get("id"):
            if meta.get("timeout", 1) <= 0:  # unattended run: answered at once, nobody to alert
                return
            self._add(Alert("al_" + meta["id"], "attention", meta.get("agent") or d.get("sender") or "", tid, root,
                            d.get("content") or meta.get("question", ""), d.get("ts", ""), meta["id"]))
        elif k == "attention_resolved" and meta.get("id"):
            a = self.inbox.get("al_" + meta["id"])
            if not a:
                return
            if meta.get("status") == "auto_confirmed" and meta.get("resolved_by") == "timeout":
                a.kind, a.ts = "auto_confirmed", d.get("ts", a.ts)
                a.text = f"Nobody answered, so '{meta.get('answer') or ''}' was taken: {meta.get('question') or a.text}"
            else:
                self.inbox.pop(a.id, None)
        elif k == "error" and tid:
            self._add(Alert(f"al_err_{tid}", "error", d.get("sender") or "", tid, root, d.get("content") or "",
                            d.get("ts", "")))
        elif k == "done" and tid and tid == root and (meta.get("status") or "done") == "done":
            t = self.bus.tasks.get(tid)
            if t and getattr(t, "unattended", False):
                return
            self._add(Alert(f"al_done_{tid}", "done", d.get("sender") or "", tid, root,
                            (t.name if t else d.get("task_name") or tid) + " finished", d.get("ts", "")))
        elif k == "alert_acked":
            for i in meta.get("ids") or []:
                if i in self.inbox and self.inbox[i].kind != "attention":
                    self.inbox.pop(i)
        elif k in CLEARS and tid:
            for i in [i for i, a in self.inbox.items() if a.root_id == tid and a.kind != "attention"]:
                self.inbox.pop(i)

    def _add(self, a: Alert) -> None:
        self.inbox.pop(a.id, None)  # newest last
        self.inbox[a.id] = a

    # ---- queries / actions ----------------------------------------------
    def alerts_pending(self, agent: str | None = None, root_id: str | None = None) -> list[Alert]:
        return [a for a in self.inbox.values()
                if (agent is None or a.agent == agent) and (root_id is None or a.root_id == root_id)]

    def by_agent(self) -> dict[str, dict]:
        """agent -> {count, kinds} for the dashboard / office blink."""
        out: dict[str, dict] = {}
        for a in self.inbox.values():
            o = out.setdefault(a.agent, {"count": 0, "kinds": []})
            o["count"] += 1
            if a.kind not in o["kinds"]:
                o["kinds"].append(a.kind)
        return out

    def ack(self, ids: list[str] | None = None, agent: str | None = None, root_id: str | None = None,
            by: str = "user") -> list[str]:
        """Dismiss alerts (all matching when ids is None). Open questions stay until they are answered."""
        pick = [a for a in self.alerts_pending(agent, root_id) if ids is None or a.id in ids]
        done = [a.id for a in pick if a.kind != "attention"]
        if done:
            self.bus.emit(Event("alert_acked", by, None, None, None,
                                f"{len(done)} alert(s) dismissed", {"ids": done}))
        return done
