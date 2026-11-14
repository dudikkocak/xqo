"""Talking to agents while a task runs: Ask / Steer messages, urgent replies, pause and resume.

- Ask: a question; the agent answers with reply_user and keeps its plan.
- Steer: a change of instructions; the agent adapts (the planner re-plans or hands off as needed).
- Messages wait in the receiving agent's inbox and join its context at its next turn, so the work in
  progress is not broken. Urgent ones go first; an urgent Ask also gets an immediate answer from a
  side call on the agent's current conversation.
- A message to an agent that is waiting on the user answers that request instead.
- A message to an agent (or department) that is not at work on the task: an Ask is answered by that agent
  straight away from the task's record, without reopening the task; a Steer goes to the entry agent, which
  is told to pass it on to the named agent.
- Messages can carry attachments (images, PDFs, text files). They are stored with the task's own
  attachments, so agents can forward them, and join the agent's context as content blocks.
- Pause holds a task tree: model calls in progress finish, the next ones wait; test runs are stopped and
  auto-confirm timers freeze. Resume releases it with a short note (plus Steer messages sent meanwhile).
- A finished (or no longer running) task is reopened by a message or Resume: it continues in the same
  task and workspace with its earlier message flow as context.
"""
from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from .attachments import describe
from .models import Event, new_id

if TYPE_CHECKING:
    from .agent import Agent
    from .bus import MessageBus
    from .models import Task

KINDS = ("ask", "steer")
LIVE = {"running", "needs_attention", "paused"}

REPLY_USER_TOOL = {
    "name": "reply_user",
    "description": "Send a short message to the human operator (shown as a chat reply). Use it to answer the "
                   "user's questions and to acknowledge new instructions. It does not end your task.",
    "input_schema": {"type": "object",
                     "properties": {"message": {"type": "string", "description": "Markdown is rendered."}},
                     "required": ["message"], "additionalProperties": False},
}


DELEGATE = (" If it reports a bug or a code or runtime error, do not fix it yourself: hand the fix to the right "
            "department (for code, engineering) with handover_task.")


def delegates(agent: Agent | None) -> bool:
    """Agents that coordinate rather than change code: limited writes (write_paths) or a department head."""
    return bool(agent and (agent.write_paths is not None or agent.is_head))


def render(msg: dict, delegate: bool = False) -> str:
    """How a queued message reads to the agent. delegate: remind a coordinating agent to hand fixes on."""
    att = (f"\n\nAttached above: {describe(msg['attachments'])}. To pass any of them on, list their names in "
           "the attachments field of ask_agent or handover_task.") if msg.get("attachments") else ""
    if msg["kind"] == "note":
        return msg["text"] + att
    to = f" (addressed to {msg['to']})" if msg.get("to") and msg.get("redirected") else ""
    text = msg["text"] or "(no text, see the attachments)"
    if msg["kind"] == "steer" and to:
        who = msg.get("to_agent") or msg["to"]
        return (f"New instruction from the user (steer, id {msg['id']}) for {msg['to']}, who is not at work on "
                f"this task right now:\n{text}{att}\n\nDo not carry it out yourself: pass it on to '{who}' with "
                "ask_agent now, then adapt your plan to the result. Acknowledge briefly with reply_user.")
    if msg["kind"] == "ask":
        return (f"Message from the user (ask, id {msg['id']}){to}:\n{text}{att}\n\n"
                "Answer with the reply_user tool, then carry on with your task as planned."
                + (DELEGATE if delegate else ""))
    return (f"New instruction from the user (steer, id {msg['id']}){to}:\n{text}{att}\n\n"
            "Change your plan to follow it: redo, re-plan or hand off work as needed. Acknowledge briefly with "
            "reply_user, then continue." + (DELEGATE if delegate else ""))


class Chat:
    def __init__(self, bus: MessageBus):
        self.bus = bus
        self.inbox: dict[str, list[dict]] = {}      # task id -> queued messages
        self.paused: dict[str, dict] = {}            # root id -> {since, by, reason, prev}
        self._gates: dict[str, asyncio.Event] = {}  # root id -> set while running
        self._runs: dict[str, set[asyncio.Task]] = {}  # root id -> sandbox runs to stop on pause
        self.convos: dict[str, tuple[Agent, list]] = {}  # task id -> (agent, its live message list)

    # ---- pause / resume -------------------------------------------------
    def is_paused(self, root_id: str) -> bool:
        return root_id in self.paused

    def pause(self, root_id: str, by: str = "user", reason: str = "") -> Task:
        root = self._root(root_id)
        if not self.bus.is_running(root_id):
            raise ValueError(f"{root.name} is not running (status: {root.status})")
        if root_id in self.paused:
            return root
        self.paused[root_id] = {"since": time.monotonic(), "by": by, "reason": reason, "prev": root.status}
        self._gates.setdefault(root_id, asyncio.Event()).clear()
        root.status = "paused"
        stopped = 0
        for job in list(self._runs.get(root_id, ())):
            if not job.done():
                job.cancel()
                stopped += 1
        self.bus.emit(Event("task_paused", by, None, root_id, None,
                            f"Paused by {by}" + (f": {reason}" if reason else ""),
                            {"by": by, "reason": reason, "runs_stopped": stopped}))
        return root

    def resume(self, root_id: str, by: str = "user", note: str = "") -> tuple[Task, bool]:
        """Release a paused task, or reopen one that is no longer running. Returns (task, reopened)."""
        root = self._root(root_id)
        if not self.bus.is_running(root_id):
            return self.reopen(root_id, note or "Resume the task where it stopped.", "steer", by), True
        info = self.paused.pop(root_id, None)
        if not info:
            raise ValueError(f"{root.name} is not paused")
        secs = int(time.monotonic() - info["since"])
        root.status = info["prev"] if info["prev"] != "paused" else "running"
        for t in self._live(root_id):  # every agent at work gets a short note at its next turn
            self._queue(t.id, {"kind": "note", "id": new_id("msg"), "urgent": True,
                               "text": f"Note: the task was paused by {info['by']} for {secs}s and has resumed. "
                                       "Check the workspace before continuing; test runs in progress were "
                                       "stopped, so re-run them if needed."})
        self._gates[root_id].set()
        self.bus.emit(Event("task_resumed", by, None, root_id, None, f"Resumed by {by} after {secs}s",
                            {"by": by, "paused_seconds": secs}))
        return root, False

    async def gate(self, task: Task, agent: Agent) -> None:
        """Before a model call: wait while the task tree is paused."""
        ev = self._gates.get(task.root_id)
        if task.root_id not in self.paused or ev is None:
            return
        agent.waiting += 1
        try:
            while task.root_id in self.paused:
                await ev.wait()
        finally:
            agent.waiting -= 1

    async def track_run(self, task: Task, coro):
        """Run a sandbox job that a pause stops. Raises RunStopped when that happens."""
        job = asyncio.ensure_future(coro)
        runs = self._runs.setdefault(task.root_id, set())
        runs.add(job)
        try:
            return await asyncio.shield(job)
        except asyncio.CancelledError:
            if job.cancelled() and task.root_id in self.paused:
                raise RunStopped("the run was stopped because the task was paused; run it again now") from None
            job.cancel()
            raise
        finally:
            runs.discard(job)

    def clear(self, root_id: str) -> None:
        """A root run ended: drop its pause state and leftover messages."""
        self.paused.pop(root_id, None)
        gate = self._gates.pop(root_id, None)
        if gate:
            gate.set()
        self._runs.pop(root_id, None)
        for tid in [k for k in self.inbox if (t := self.bus.tasks.get(k)) is None or t.root_id == root_id]:
            self.inbox.pop(tid, None)

    # ---- messages -------------------------------------------------------
    def targets(self, root_id: str, to: str | None) -> tuple[list[Task], bool]:
        """Live tasks that should receive a message for `to` (None/'all' = the task's entry agent).
        Returns (tasks, redirected): redirected when the named agent is not at work on this task."""
        root = self._root(root_id)
        live = self._live(root_id)
        if not to or to in ("all", "*"):
            return [root], False
        name = self.resolve(to)
        mine = [t for t in live if t.assignee == name]
        dept = (getattr(self.bus, "departments", None) or {}).get(to.lstrip("@"))
        if not mine and dept and to.lstrip("@") not in self.bus.agents:  # the head is idle: a member at work
            mine = [t for t in live if t.assignee in dept.members]
        return (mine, False) if mine else ([root], True)

    def resolve(self, to: str | None) -> str | None:
        """Agent name for a recipient: an agent, or a department (its head handles the message)."""
        if not to:
            return None
        name = to.lstrip("@")
        depts = getattr(self.bus, "departments", None) or {}
        if name not in self.bus.agents and name in depts:
            return depts[name].head
        return self.bus.agent_id(name) or name  # an id, else a nickname

    async def send(self, root_id: str, text: str, kind: str = "ask", to: str | None = None,
                   urgent: bool = False, by: str = "user", files: list[dict] | None = None,
                   answers: bool = True) -> dict:
        """Deliver an Ask or Steer message. Reopens the task when it is not running.

        files: attachments checked by attachments.validate; they are stored with the root task.
        answers=False: never take the message as the answer to an open question (messages from the public)."""
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        text = text.strip()
        if not text and not files:
            raise ValueError("the message is empty")
        root = self._root(root_id)
        to = (to or "").strip().lstrip("@") or None
        to = (self.bus.agent_id(to) or to) if to else None  # a nickname becomes the id
        self._check_target(to)
        running = self.bus.is_running(root_id)
        name = self.resolve(to)
        # the named agent is not at work on the task (or the task is over): it answers an Ask itself
        direct = kind == "ask" and name and name != root.assignee and self.targets(root_id, to)[1]
        if not running and not direct:
            self._check_reopen(root)  # fail before any file is stored
        msg = {"id": new_id("msg"), "kind": kind, "text": text, "to": to, "urgent": urgent, "by": by}
        if to and name != to:
            msg["to_agent"] = name
        if files:
            msg["attachments"] = self.bus.attachments.save(root_id, files)
            root.attachments = list(root.attachments) + msg["attachments"]
        if direct:
            self._emit_message(root, msg, [], recipient=name)
            self.bus.spawn(self.direct_answer(root, name, msg))
            return {**msg, "delivered_to": [], "answered_by": name}
        if not running:  # finished or interrupted: continue in the same task
            msg["redirected"] = bool(name and name != root.assignee)
            task = self.reopen(root_id, text, kind, by, msg)
            return {**msg, "reopened": True, "task_id": task.id}
        answered = self._answer_attention(root_id, to, text, by, msg.get("attachments")) if answers else None
        if answered:
            if msg.get("attachments"):  # the answer is text; the files follow at the agent's next turn
                self._queue(answered.task_id, {"kind": "note", "id": new_id("msg"), "urgent": True,
                                               "attachments": msg["attachments"],
                                               "text": f"The user attached these files to their answer ({msg['id']})."})
            self._emit_message(root, msg, [answered.task_id], answered=answered.id)
            return {**msg, "answered_attention": answered.id, "delivered_to": [answered.task_id]}
        if kind == "steer" and not to:  # a change of plan reaches everyone at work on the task
            tasks, redirected = self._live(root_id) or [root], False
        else:
            tasks, redirected = self.targets(root_id, to)
        msg["redirected"] = redirected
        for t in tasks:
            self._queue(t.id, dict(msg))
        self._emit_message(root, msg, [t.id for t in tasks])
        if urgent and kind == "ask":
            for t in tasks[:1]:
                self.bus.spawn(self.side_reply(t, msg))
        return {**msg, "delivered_to": [t.id for t in tasks]}

    def drain(self, task: Task) -> list[str | list[dict]]:
        """Messages for an agent's next turn, urgent first; they are removed from the inbox.

        A message without attachments is a string; one with attachments is a list of content blocks."""
        q = self.inbox.pop(task.id, [])
        q.sort(key=lambda m: not m.get("urgent"))
        return [self.content(task, m) for m in q]

    def content(self, task: Task, msg: dict) -> str | list[dict]:
        agent = self.bus.agents.get(task.assignee)
        if not msg.get("attachments") or not getattr(self.bus, "attachments", None):
            return render(msg, delegates(agent))
        if agent and agent.file_refs():  # Claude Code reads them on demand with Read
            refs = self.bus.attachments.refs(task.root_id, msg["attachments"])
            return render({**msg, "attachments": None}, delegates(agent)) + ("\n\n" + refs if refs else "")
        text = render(msg, delegates(agent))
        return self.bus.attachments.blocks(task.root_id, msg["attachments"]) + [{"type": "text", "text": text}]

    def has_mail(self, task: Task) -> bool:
        return bool(self.inbox.get(task.id))

    async def side_reply(self, task: Task, msg: dict) -> None:
        """Urgent Ask: answer now from the agent's current conversation, without touching it."""
        convo = self.convos.get(task.id)
        agent = convo[0] if convo else self.bus.agents.get(task.assignee)
        if agent is None:
            return
        messages = [dict(m) for m in (convo[1] if convo else [])]
        if messages and messages[-1]["role"] == "assistant":  # a tool call is still running: say so
            pending = [b for b in messages[-1]["content"] if getattr(b, "type", None) == "tool_use"]
            if pending:
                messages.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": b.id, "content": "(still running)"} for b in pending]})
        q = (f"URGENT message from the user (ask, id {msg['id']}):\n{msg['text'] or '(see the attachments)'}"
             + (f"\n\nAttached above: {describe(msg['attachments'])}." if msg.get("attachments") else "")
             + "\n\nAnswer right away with reply_user, based on what you know now. Your other work continues "
             "separately.")
        if msg.get("attachments") and getattr(self.bus, "attachments", None):
            q = self.bus.attachments.blocks(task.root_id, msg["attachments"]) + [{"type": "text", "text": q}]
        add_user_text(messages, q) if messages else messages.append({"role": "user", "content": q})
        replies = await self._answer(task, agent, messages, msg["id"], urgent=True)
        if replies is None:
            return
        if convo:  # the agent learns at its next turn what it already told the user
            self._queue(task.id, {"kind": "note", "id": new_id("msg"), "urgent": True,
                                  "text": f"While you worked you already answered the user's urgent question "
                                          f"{msg['id']} ({msg['text'][:300]!r}) with: {' '.join(replies)[:1500]}"})

    async def direct_answer(self, root: Task, name: str, msg: dict) -> None:
        """Ask to an agent not at work on the task: it answers from the task's record, without doing work."""
        agent = self.bus.agents[name]
        record = self.bus.transcript([e for e in self.bus.task_events(root.id) if e.get("meta", {}).get("id") != msg["id"]])
        q = (f"The user asks you about task {root.name}. You are not working on it right now. Its message flow "
             f"so far:\n\n{record}\n\n"
             f"Message from the user (ask, id {msg['id']}), addressed to you:\n{msg['text'] or '(see the attachments)'}"
             + (f"\nAttached above: {describe(msg['attachments'])}." if msg.get("attachments") else "")
             + "\n\nAnswer with the reply_user tool, from this record and what you know. Do not start any work.")
        if msg.get("attachments") and getattr(self.bus, "attachments", None):
            q = self.bus.attachments.blocks(root.id, msg["attachments"]) + [{"type": "text", "text": q}]
        replies = await self._answer(root, agent, [{"role": "user", "content": q}], msg["id"])
        if replies and self.bus.is_running(root.id):  # the entry agent learns what was said at its next turn
            self._queue(root.id, {"kind": "note", "id": new_id("msg"),
                                  "text": f"Note: {name} answered the user's question {msg['id']} "
                                          f"({msg['text'][:300]!r}) with: {' '.join(replies)[:1500]}"})

    async def _answer(self, task: Task, agent: Agent, messages: list, msg_id: str,
                      urgent: bool = False) -> list[str] | None:
        """One reply_user call outside the agent's work; its answers become chat replies. None on failure."""
        try:
            resp = await agent.llm.create(model=agent.model, system=agent._system(), messages=messages,
                                          tools=[REPLY_USER_TOOL], effort=agent.effort, agent=agent.name)
        except Exception as e:  # never break the task over a side answer
            self.bus.emit(Event("error", agent.name, "user", task.id, task.parent_id, f"chat reply failed: {e}"))
            return None
        replies = [b.input.get("message", "") for b in resp.content if getattr(b, "type", None) == "tool_use"
                   and b.name == "reply_user"] or [b.text for b in resp.content if getattr(b, "type", None) == "text"]
        for r in replies:
            if r.strip():
                self.reply(task, agent.name, r, msg_id, urgent=urgent)
        return replies

    def reply(self, task: Task, agent: str, text: str, in_reply_to: str | None = None, urgent: bool = False):
        self.bus.emit(Event("agent_reply", agent, "user", task.id, task.parent_id, text,
                            {"in_reply_to": in_reply_to, **({"urgent": True} if urgent else {})}))

    # ---- reopen ---------------------------------------------------------
    def reopen(self, root_id: str, text: str, kind: str = "steer", by: str = "user",
               msg: dict | None = None) -> Task:
        """Continue a task that is not running, in the same task and workspace. Start it with bus.spawn."""
        root = self._root(root_id)
        self._check_reopen(root)
        prev = root.status
        events = self.bus.task_events(root_id)
        root.context = (f"This task ran before and ended with status '{prev}'. The workspace and notes board are "
                        f"as it left them. Its message flow so far:\n\n{self.bus.transcript(events)}\n\n"
                        "The user's new message follows. "
                        + ("For a question, answer with reply_user and do more work only if it needs it."
                           if kind == "ask" else "Continue the task following it.")
                        + (DELEGATE if delegates(self.bus.agents.get(root.assignee)) else ""))
        root.status, root.paused = "pending", 0.0
        self.paused.pop(root_id, None)
        msg = msg or {"id": new_id("msg"), "kind": kind, "text": text, "to": None, "urgent": False, "by": by}
        self._queue(root.id, dict(msg))  # delivered at the first turn, like any chat message
        self._emit_message(root, msg, [root.id])
        self.bus.emit(Event("task_reopened", by, root.assignee, root_id, None,
                            f"Reopened by {by} (was {prev})", {"by": by, "previous_status": prev, "kind": kind}))
        self.bus.spawn(self.bus._run(root))
        return root

    # ---- helpers --------------------------------------------------------
    def _check_reopen(self, root: Task) -> None:
        if self.bus.is_running(root.id):
            raise ValueError(f"{root.name} is already running")
        if self.bus.lifecycle and self.bus.lifecycle.entry(root.id):
            raise ValueError(f"{root.name} is in the trash; restore it first")

    def _root(self, root_id: str) -> Task:
        t = self.bus.tasks.get(root_id)
        if t is None or t.root_id != root_id:
            raise KeyError(f"unknown task {root_id}")
        return t

    def _live(self, root_id: str) -> list[Task]:
        return [t for t in self.bus.tasks.values() if t.root_id == root_id and t.status in LIVE]

    def _check_target(self, to: str | None) -> None:
        if to and self.resolve(to) not in self.bus.agents:
            depts = list(getattr(self.bus, "departments", None) or {})
            known = [f"{n} ({a.nickname})" if getattr(a, "nickname", None) else n for n, a in self.bus.agents.items()]
            raise KeyError(f"unknown agent {to}. Known: {', '.join(known + depts)}")

    def _queue(self, task_id: str, msg: dict) -> None:
        self.inbox.setdefault(task_id, []).append(msg)

    def _answer_attention(self, root_id: str, to: str | None, text: str, by: str, attachments=None):
        for a in self.bus.attention.pending():
            t = self.bus.tasks.get(a.task_id)
            if t and t.root_id == root_id and (not to or a.agent == self.resolve(to)):
                if attachments:
                    text = (text + "\n\n" if text else "") + f"(attached: {describe(attachments)})"
                self.bus.attention.resolve(a.id, text, by=f"{by} (chat)")
                return a
        return None

    def _emit_message(self, root: Task, msg: dict, delivered: list[str], answered: str | None = None,
                      recipient: str | None = None) -> None:
        meta = {k: msg.get(k) for k in ("id", "kind", "to", "urgent", "by")}
        if msg.get("attachments"):
            meta["attachments"] = msg["attachments"]
        meta.update({"delivered_to": delivered, **({"answered_attention": answered} if answered else {}),
                     **({"redirected": True} if msg.get("redirected") else {})})
        target = (self.bus.tasks.get(delivered[0]) if delivered else None) or root
        to = recipient or ("*" if len(delivered) > 1 else target.assignee)
        self.bus.emit(Event("user_message", msg.get("by", "user"), to, target.id, target.parent_id, msg["text"], meta))


class RunStopped(RuntimeError):
    pass


def add_user_text(messages: list, text: str | list[dict]) -> None:
    """Append text (or content blocks) to the conversation as user content, merged into a trailing user turn."""
    if messages and messages[-1]["role"] == "user":
        last = messages[-1]
        content = last["content"]
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        else:
            content = list(content)
        content += [{"type": "text", "text": text}] if isinstance(text, str) else text
        messages[-1] = {**last, "content": content}
    else:
        messages.append({"role": "user", "content": text})
