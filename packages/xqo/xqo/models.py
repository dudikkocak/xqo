from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


@dataclass
class Event:
    kind: str  # task|direct|reply|broadcast|offer|handover|result|artifact|llm|error|done
    sender: str
    recipient: str | None = None
    task_id: str | None = None
    parent_task_id: str | None = None
    content: str = ""
    meta: dict = field(default_factory=dict)
    id: str = field(default_factory=lambda: new_id("evt"))
    ts: str = field(default_factory=utcnow)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Task:
    prompt: str
    assignee: str
    requester: str = "user"
    parent_id: str | None = None
    chain: list[str] = field(default_factory=list)  # agents already involved, to prevent loops
    depth: int = 0
    id: str = field(default_factory=lambda: new_id("task"))
    created: str = field(default_factory=utcnow)
    status: str = "pending"  # pending|running|needs_attention|paused|done|error|cancelled|interrupted
    result: str | None = None
    root_id: str | None = None  # top-level user task this belongs to
    title: str = ""  # one-line summary
    paused: float = 0.0  # seconds spent waiting on the user (excluded from the time limit)
    context: str = ""  # extra background for the agent (a forked task's transcript)
    forked_from: dict | None = None  # {task_id, task_name, event_id} when this task is a fork
    attachments: list = field(default_factory=list)  # [{name, media_type, kind, bytes}] stored per root task
    tests: dict = field(default_factory=dict)  # {runs, failed, last_exit} from run_code in this task (and below)
    usage: dict = field(default_factory=dict)  # Claude Code sessions of this task: {cost_usd, tokens, turns, ...}
    schedule: dict | None = None  # {id, name, run, reason} when a schedule started this task (scheduler.py)
    limits: dict = field(default_factory=dict)  # a scheduled run's own max_turns / max_tokens / max_cost_usd
    unattended: bool = False  # questions to the user take their default answer at once (scheduled runs)
    archived: bool = False  # hidden from the task list until unarchived (lifecycle.py)

    def __post_init__(self):
        self.root_id = self.root_id or self.id
        self.title = self.title or short_title(self.prompt)

    @property
    def name(self) -> str:
        """Display name: 'task_<YYMMDDhhmm> : <title>'."""
        return f"{self.id} : {self.title}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("context")
        return {**d, "name": self.name}


def short_title(text: str, limit: int = 60) -> str:
    """Heuristic one-line summary: first meaningful line, cut at a word boundary."""
    for line in (text or "").splitlines():
        line = re.sub(r"^(?:task from \S+ \(task id [^)]*\):|[#>*\-\s]+)", "", line.strip(), flags=re.I).strip()
        if line:
            break
    else:
        return "untitled"
    line = re.sub(r"\s+", " ", line)
    if len(line) <= limit:
        return line.rstrip(".:")
    cut = line[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(".,:;") + "…"


def task_stamp(now: datetime | None = None) -> str:
    """task_<YYMMDDhhmm> in local time."""
    return "task_" + (now or datetime.now()).strftime("%y%m%d%H%M")
