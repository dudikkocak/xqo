"""Scheduled and repeating tasks: an in-process scheduler that starts ordinary tasks on a timetable.

A schedule fires through the same path as a task submitted on the dashboard (Bus.create + Bus._run), so every
run is a normal task: live viewer, replay, fork, archive and trash all work on it. The run's task carries
{"schedule": {id, name, run}} and shows a clock badge in the dashboard.

Triggers ("trigger" in a schedule):
  {"kind": "once", "at": "2026-11-01T09:00"}         one run; the schedule is finished afterwards
  {"kind": "interval", "every": "6h"}                every N s/m/h/d/w (60 s at least), counted from "start"
  {"kind": "cron", "expr": "0 2 * * 1-5"}            5-field cron: minute hour day-of-month month day-of-week
  {"kind": "feed", "rule": "disk-full"}              started by a feed rule (feeds/), never by the clock; these
                                                     schedules come from "feed_rules" in agents.json
Times are wall-clock times in the schedule's "tz" (an IANA name such as "Europe/Berlin"; default: the server's).

Where they live: schedules made in the dashboard / API / CLI are kept in <artifacts>/schedules.json together
with every schedule's run state (next run, last run, run count, failures), written atomically on each change.
"schedules": [...] in agents.json adds config-owned schedules: the dashboard can pause, resume and run them, but
their definition only changes in the file. A restart reloads everything and recomputes the next runs.

Policies per schedule:
  missed     "run_once" (default): a run missed while the server was down runs once at start-up if it is no
             older than "grace" (default 1h); "skip": missed runs are only logged. Missed runs never pile up.
  overlap    the previous run is still running, paused or waiting on the user: "skip" (default, logged),
             "queue" (one run waits and starts when the previous one ends) or "parallel".
  limits     {"max_turns", "max_tokens", "max_cost_usd", "honor_no_limits"} for the run's task tree only.
             max_cost_usd cancels the run once its estimated cost reaches the cap. The dashboard's "No limits"
             hour is ignored by scheduled runs unless honor_no_limits is true. hq department budgets stay on.
  unattended true (default): questions to the user (ask_user, budget, time limit) take their default answer
             at once instead of waiting for the attention timeout. false: they wait like any task.
  max_failures  the schedule pauses itself after this many failed runs in a row (default 3; 0 = never).
  end        {"after_runs": N, "until": "2026-12-31"}: the schedule finishes after N runs or that time.
  archive_after  e.g. "7d": finished runs of this schedule older than that are archived (lifecycle.py).

Only one process runs the scheduler for a data dir: it takes <artifacts>/scheduler.lock when it starts.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from xqo.api import AttachmentStore
from xqo.api import Event, new_id, utcnow

if TYPE_CHECKING:
    from xqo.api import MessageBus

ACTIVE = {"pending", "running", "needs_attention", "paused"}  # a run in one of these blocks an overlapping run
FAILED = {"error", "cancelled", "interrupted"}
OVERLAP = ("skip", "queue", "parallel")
MISSED = ("run_once", "skip")
LIMIT_KEYS = ("max_turns", "max_tokens", "max_cost_usd", "honor_no_limits")
LOCKED = ("config", "feed")  # defined in agents.json: only pause / resume / run in the dashboard
MIN_INTERVAL = 60.0
TICK = 30.0  # longest sleep between checks (edits wake the loop at once)
COST_CHECK = 2.0  # seconds between cost-cap checks of a running scheduled task
ARCHIVE_CHECK = 300.0
EDITABLE = {"name", "enabled", "agent", "prompt", "trigger", "tz", "overlap", "missed", "grace", "limits",
            "unattended", "max_failures", "end", "archive_after"}


# ---- durations, times and cron ---------------------------------------------------------------
_DUR = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*$", re.I)
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(v, what: str = "duration") -> float:
    """'90s', '30m', '6h', '1d', '2w' or a number of seconds."""
    if isinstance(v, bool):
        raise ValueError(f"{what}: expected a duration like '30m', '6h' or '1d'")
    if isinstance(v, (int, float)):
        secs = float(v)
    else:
        m = _DUR.match(str(v or ""))
        if not m:
            raise ValueError(f"{what}: {v!r} is not a duration like '30m', '6h' or '1d'")
        secs = float(m.group(1)) * _UNITS[m.group(2).lower()]
    if secs <= 0:
        raise ValueError(f"{what} must be more than 0")
    return secs


def zone(name: str | None):
    if not name:
        return None  # the server's local time
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"unknown time zone {name!r}") from None


def parse_time(v, tz) -> float:
    """ISO date/time (naive = wall-clock in tz) or epoch seconds -> epoch seconds."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    s = str(v or "").strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s) if "T" in s or " " in s or ":" in s else \
            datetime.combine(date.fromisoformat(s), datetime.min.time())
    except ValueError:
        raise ValueError(f"{v!r} is not a date/time like 2026-11-01T09:00") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz) if tz else dt.astimezone()
    return dt.timestamp()


def iso(epoch: float | None) -> str | None:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z") \
        if epoch else None


_NAMES = {3: {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())},
          4: {d: i for i, d in enumerate("sun mon tue wed thu fri sat".split())}}
_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
_MACROS = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
           "@weekly": "0 0 * * 0", "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *"}


class Cron:
    """A standard 5-field cron expression (lists, ranges, steps, month/day names, @daily-style macros).
    When both day-of-month and day-of-week are restricted, a day matching either one fires (as in cron)."""

    def __init__(self, expr: str):
        self.expr = str(expr or "").strip()
        parts = _MACROS.get(self.expr.lower(), self.expr).split()
        if len(parts) != 5:
            raise ValueError(f"cron {self.expr!r}: expected 5 fields (minute hour day month weekday)")
        self.sets = [self._field(p, i) for i, p in enumerate(parts)]
        self.sets[4] = {d % 7 for d in self.sets[4]}  # 7 = Sunday too
        self.dom_any, self.dow_any = parts[2] == "*", parts[4] == "*"
        self.minutes, self.hours = sorted(self.sets[0]), sorted(self.sets[1])

    def _field(self, text: str, i: int) -> set[int]:
        lo, hi = _RANGES[i]
        out: set[int] = set()
        for part in text.lower().split(","):
            rng, _, step = part.partition("/")
            try:
                step_n = int(step) if step else 1
                if rng in ("*", ""):
                    a, b = lo, hi
                else:
                    a_s, dash, b_s = rng.partition("-")
                    a = self._num(a_s, i)
                    b = self._num(b_s, i) if dash else (hi if step else a)
            except ValueError:
                raise ValueError(f"cron {self.expr!r}: bad field {text!r}") from None
            if step_n < 1 or not (lo <= a <= hi and lo <= b <= hi) or a > b:
                raise ValueError(f"cron {self.expr!r}: {text!r} is out of range {lo}-{hi}")
            out.update(range(a, b + 1, step_n))
        return out

    @staticmethod
    def _num(s: str, i: int) -> int:
        return _NAMES.get(i, {}).get(s[:3]) if s[:3] in _NAMES.get(i, {}) else int(s)

    def _day_ok(self, d: date) -> bool:
        if d.month not in self.sets[3]:
            return False
        dom, dow = d.day in self.sets[2], (d.weekday() + 1) % 7 in self.sets[4]
        if self.dom_any or self.dow_any:
            return dom and dow
        return dom or dow

    def next_after(self, after: float, tz) -> float | None:
        """First matching minute strictly after `after` (epoch), as epoch seconds; None if none in 5 years."""
        start = datetime.fromtimestamp(after, tz) if tz else datetime.fromtimestamp(after).astimezone()
        start = start.replace(second=0, microsecond=0, tzinfo=None) + timedelta(minutes=1)
        day = start.date()
        for _ in range(366 * 5):
            if self._day_ok(day):
                for h in self.hours:
                    if day == start.date() and h < start.hour:
                        continue
                    for m in self.minutes:
                        local = datetime(day.year, day.month, day.day, h, m)
                        if local < start:
                            continue
                        epoch = local.replace(tzinfo=tz).timestamp() if tz else local.astimezone().timestamp()
                        if epoch > after:  # a wall-clock time skipped by a DST change maps forward
                            return epoch
            day += timedelta(days=1)
        return None


# ---- schedules ---------------------------------------------------------------------------------
@dataclass
class Schedule:
    name: str
    prompt: str
    trigger: dict
    agent: str | None = None
    enabled: bool = True
    source: str = "ui"  # ui | config
    tz: str | None = None
    overlap: str = "skip"
    missed: str = "run_once"
    grace: float = 3600.0
    limits: dict = field(default_factory=dict)
    unattended: bool = True
    max_failures: int = 3
    end: dict = field(default_factory=dict)  # {"after_runs": N, "until": epoch}
    archive_after: float | None = None
    attachments: list = field(default_factory=list)  # [{name, media_type, kind, bytes}] kept with the schedule
    id: str = field(default_factory=lambda: new_id("sch"))
    created: str = field(default_factory=utcnow)
    updated: str = field(default_factory=utcnow)

    def to_dict(self) -> dict:
        return asdict(self)


def _bool(v, what: str) -> bool:
    if not isinstance(v, bool):
        raise ValueError(f"{what} must be true or false")
    return v


def normalize(d: dict, source: str = "ui", base: Schedule | None = None, has_files: bool = False) -> Schedule:
    """Check a schedule definition (from the API or agents.json) and return a Schedule. Raises ValueError."""
    if not isinstance(d, dict):
        raise ValueError("a schedule must be an object")
    known = EDITABLE | {"id", "attachments", "created", "updated", "source", "every", "cron", "at"}
    bad = set(d) - known
    if bad:
        raise ValueError(f"unknown schedule setting(s) {', '.join(sorted(bad))}")
    cur = base.to_dict() if base else {}
    v = {**cur, **{k: d[k] for k in d if k in EDITABLE}}
    trig = d.get("trigger") if "trigger" in d else None  # shorthands: "every", "cron", "at"
    for k, kind, key in (("every", "interval", "every"), ("cron", "cron", "expr"), ("at", "once", "at")):
        if k in d:
            if trig is not None:
                raise ValueError("give one of trigger, every, cron or at")
            trig = {"kind": kind, key: d[k]}
    if trig is not None:
        v["trigger"] = trig
    name = str(v.get("name") or "").strip()
    prompt = str(v.get("prompt") or "").strip()
    if not name:
        raise ValueError("a schedule needs a name")
    if len(name) > 120:
        raise ValueError("the schedule name is longer than 120 characters")
    if not prompt and not (has_files or (base and base.attachments)):
        raise ValueError(f"schedule {name!r}: write a prompt")
    tz = v.get("tz") or None
    zone(tz)
    t = v.get("trigger")
    if not isinstance(t, dict) or t.get("kind") not in ("once", "interval", "cron", "feed"):
        raise ValueError(f"schedule {name!r}: trigger must be once, interval or cron")
    t = dict(t)
    if t["kind"] == "feed":
        if source != "feed":
            raise ValueError(f"schedule {name!r}: feed triggers come from feed_rules in agents.json")
        t = {"kind": "feed", "rule": str(t.get("rule") or "")}
    elif t["kind"] == "once":
        t = {"kind": "once", "at": parse_time(t.get("at"), zone(tz))}
    elif t["kind"] == "interval":
        every = parse_duration(t.get("every"), "every")
        if every < MIN_INTERVAL:
            raise ValueError(f"schedule {name!r}: the shortest interval is {int(MIN_INTERVAL)}s")
        t = {"kind": "interval", "every": every,
             **({"start": parse_time(t["start"], zone(tz))} if t.get("start") not in (None, "") else {})}
    else:
        t = {"kind": "cron", "expr": Cron(t.get("expr")).expr}
    overlap, missed = v.get("overlap") or "skip", v.get("missed") or "run_once"
    if overlap not in OVERLAP:
        raise ValueError(f"overlap must be one of {', '.join(OVERLAP)}")
    if missed not in MISSED:
        raise ValueError(f"missed must be one of {', '.join(MISSED)}")
    limits = dict(v.get("limits") or {})
    bad = set(limits) - set(LIMIT_KEYS)
    if bad:
        raise ValueError(f"limits: unknown setting(s) {', '.join(sorted(bad))}; known: {', '.join(LIMIT_KEYS)}")
    for k in ("max_turns", "max_tokens"):
        if limits.get(k) is not None:
            if isinstance(limits[k], bool) or not isinstance(limits[k], (int, float)) or limits[k] < 1:
                raise ValueError(f"limits.{k} must be a positive number")
            limits[k] = int(limits[k])
    if limits.get("max_cost_usd") is not None:
        c = limits["max_cost_usd"]
        if isinstance(c, bool) or not isinstance(c, (int, float)) or c <= 0:
            raise ValueError("limits.max_cost_usd must be a positive number")
    if "honor_no_limits" in limits:
        _bool(limits["honor_no_limits"], "limits.honor_no_limits")
    limits = {k: x for k, x in limits.items() if x is not None}
    end = dict(v.get("end") or {})
    bad = set(end) - {"after_runs", "until"}
    if bad:
        raise ValueError(f"end: unknown setting(s) {', '.join(sorted(bad))}; known: after_runs, until")
    if end.get("after_runs") is not None:
        if isinstance(end["after_runs"], bool) or not isinstance(end["after_runs"], int) or end["after_runs"] < 1:
            raise ValueError("end.after_runs must be a whole number of at least 1")
    if end.get("until") not in (None, ""):
        end["until"] = parse_time(end["until"], zone(tz))
    end = {k: x for k, x in end.items() if x not in (None, "")}
    mf = v.get("max_failures", 3)
    if isinstance(mf, bool) or not isinstance(mf, int) or mf < 0:
        raise ValueError("max_failures must be a whole number (0 = never pause)")
    aa = v.get("archive_after")
    s = Schedule(name=name, prompt=prompt, trigger=t, agent=v.get("agent") or None,
                 enabled=_bool(v.get("enabled", True), "enabled"), source=source, tz=tz,
                 overlap=overlap, missed=missed, grace=parse_duration(v.get("grace", 3600), "grace"),
                 limits=limits, unattended=_bool(v.get("unattended", True), "unattended"), max_failures=mf,
                 end=end, archive_after=parse_duration(aa, "archive_after") if aa not in (None, "", 0) else None,
                 attachments=list(base.attachments) if base else [])
    if base:
        s.id, s.created = base.id, base.created
    elif d.get("id"):
        sid = str(d["id"])
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", sid):
            raise ValueError(f"schedule id {sid!r}: use letters, digits, '.', '_' or '-'")
        s.id = sid
    return s


def next_after(s: Schedule, after: float, anchor: float | None = None) -> float | None:
    """The next run time strictly after `after`. An interval without a "start" counts from `anchor`."""
    t, tz = s.trigger, zone(s.tz)
    if t["kind"] == "feed":
        return None
    if t["kind"] == "once":
        return t["at"] if t["at"] > after else None
    if t["kind"] == "interval":
        anchor = t.get("start") or anchor or parse_time(s.created, tz)
        if after < anchor:
            return anchor
        return anchor + (math.floor((after - anchor) / t["every"]) + 1) * t["every"]
    return Cron(t["expr"]).next_after(after, tz)


def describe(s: Schedule) -> str:
    t = s.trigger
    if t["kind"] == "feed":
        return f"on feed rule {t['rule']}"
    if t["kind"] == "once":
        return "once at " + (iso(t["at"]) or "")
    if t["kind"] == "interval":
        e = t["every"]
        for unit, n in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60)):
            if e % n == 0:
                return f"every {int(e // n)}{unit}"
        return f"every {int(e)}s"
    tz = f" ({s.tz})" if s.tz else ""
    m = re.fullmatch(r"(\d{1,2}) ([\d,]+) \* \* (\S+)", t["expr"])  # "at 09:00 on Mon-Fri" for the common shapes
    if m:
        try:
            days = Cron(t["expr"]).sets[4]
        except ValueError:
            days = None
        if days is not None:
            at = ", ".join(f"{int(h):02d}:{int(m.group(1)):02d}" for h in m.group(2).split(","))
            names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
            when = ("daily" if len(days) == 7 else "Mon-Fri" if days == {1, 2, 3, 4, 5} else
                    "Sat, Sun" if days == {0, 6} else ", ".join(names[d] for d in (*sorted(days - {0}), *({0} & days))))
            return f"{when} at {at}{tz}"
    return "cron " + t["expr"] + tz


# ---- the scheduler -----------------------------------------------------------------------------
class Scheduler:
    def __init__(self, bus: MessageBus, root: str | Path, config: list | None = None, clock=time.time):
        self.bus = bus
        self.root = Path(root)
        self.path = self.root / "schedules.json"
        self.files = AttachmentStore(self.root / ".schedules")
        self.clock = clock
        self.schedules: dict[str, Schedule] = {}
        self.state: dict[str, dict] = {}
        self.running = False  # the loop runs in this process
        self.lock_error = ""
        self._wake: asyncio.Event | None = None
        self._loop_task: asyncio.Task | None = None
        self._lock_fd = None
        self._jobs: dict[str, set[str]] = {}  # schedule id -> root task ids of its runs still going
        self.feed_meta: dict[str, dict] = {}  # schedule id -> {"rule", "mode"} for schedules of feed rules
        self._last_archive = 0.0
        self._load(config or [])

    # ---- persistence ----------------------------------------------------
    def _load(self, config: list) -> None:
        data = {}
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
        for d in (data.get("schedules") or {}).values():
            try:
                s = normalize(d, "ui", has_files=bool(d.get("attachments")))
                s.id, s.created, s.updated = d["id"], d.get("created", s.created), d.get("updated", s.updated)
                s.attachments = d.get("attachments") or []
                self.schedules[s.id] = s
            except (ValueError, KeyError):
                continue  # a hand-edited entry that no longer parses: left out rather than stopping the server
        if not isinstance(config, list):
            raise ValueError("schedules: expected a list of schedules")
        for d in config:
            if not isinstance(d, dict):
                raise ValueError("schedules: each schedule must be an object")
            try:
                s = normalize({"id": d.get("id") or "cfg-" + re.sub(r"[^a-z0-9]+", "-", str(d.get("name", "")).lower())
                               .strip("-")[:40], **{k: x for k, x in d.items() if k != "id"}}, "config")
            except ValueError as e:
                raise ValueError(f"schedules: {e}") from None
            if s.id in self.schedules and self.schedules[s.id].source == "config":
                raise ValueError(f"schedules: two schedules have the id {s.id!r}")
            self.schedules[s.id] = s
        self.state = {k: v for k, v in (data.get("state") or {}).items()
                      if k in self.schedules or k.startswith("feed-")}  # feed rules register later
        for sid, st in self.state.items():  # config schedules: the dashboard's pause / resume is kept here
            if "enabled" in st and sid in self.schedules and self.schedules[sid].source == "config":
                self.schedules[sid].enabled = bool(st["enabled"])

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for sid, s in self.schedules.items():
            if s.source in LOCKED:
                self.state.setdefault(sid, {})["enabled"] = s.enabled
        data = {"schedules": {k: s.to_dict() for k, s in self.schedules.items() if s.source not in LOCKED},
                "state": self.state}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def st(self, sid: str) -> dict:
        return self.state.setdefault(sid, {"runs": 0, "failures_in_row": 0})

    # ---- views ----------------------------------------------------------
    def view(self, s: Schedule) -> dict:
        st = self.st(s.id)
        d = s.to_dict()
        t = dict(s.trigger)
        for k in ("at", "start"):
            if k in t:
                t[k] = iso(t[k])
        d["trigger"] = t
        if "until" in s.end:
            d["end"] = {**s.end, "until": iso(s.end["until"])}
        return {**d, "summary": describe(s), "running": sorted(self._jobs.get(s.id, ())),
                "next_run": iso(st.get("next_run")) if s.enabled else None,
                "last_run": iso(st.get("last_run")), "last_status": st.get("last_status"),
                "last_task": st.get("last_task"), "runs": st.get("runs", 0),
                "failures_in_row": st.get("failures_in_row", 0), "queued": bool(st.get("queued")),
                "finished": bool(st.get("finished")), "paused_reason": st.get("paused_reason", ""),
                "agent_missing": bool(s.agent and s.agent not in self.bus.agents)}

    def list(self) -> list[dict]:
        return [self.view(s) for s in sorted(self.schedules.values(), key=lambda s: s.name.lower())]

    def get(self, sid: str) -> Schedule:
        if sid not in self.schedules:
            raise KeyError(f"unknown schedule {sid}")
        return self.schedules[sid]

    def runs(self, sid: str, limit: int = 100) -> list[dict]:
        """Recent schedule_run events of one schedule (started / skipped / missed / finished), newest first."""
        self.get(sid)
        out = [d for d in self.bus.store.by_kind_prefix("schedule_") if (d.get("meta") or {}).get("schedule_id") == sid]
        return out[::-1][:limit]

    # ---- changes ----------------------------------------------------------
    def _check_agent(self, s: Schedule) -> None:
        if s.agent and s.agent not in self.bus.agents:
            raise ValueError(f"unknown agent {s.agent}")

    def create(self, d: dict, files: list[dict] | None = None, by: str = "user") -> dict:
        s = normalize({k: v for k, v in d.items() if k != "id"}, "ui", has_files=bool(files))
        self._check_agent(s)
        if files:
            s.attachments = self.files.save(s.id, files)
        self.schedules[s.id] = s
        self.state[s.id] = {"runs": 0, "failures_in_row": 0}
        self._plan(s, self.clock())
        self._event("schedule_created", s, f"Schedule {s.name} created ({describe(s)})", by=by)
        self.save()
        self.poke()
        return self.view(s)

    def update(self, sid: str, d: dict, by: str = "user") -> dict:
        s = self.get(sid)
        if s.source in LOCKED:
            if set(d) - {"enabled"}:
                raise PermissionError(f"{s.name} comes from agents.json: change it there (only pause/resume here)")
            return self.set_enabled(sid, bool(d.get("enabled")), by)
        new = normalize(d, "ui", base=s)
        self._check_agent(new)
        new.updated = utcnow()
        timing = (new.trigger, new.tz) != (s.trigger, s.tz)
        self.schedules[sid] = new
        st = self.st(sid)
        if timing or (new.enabled and not s.enabled):
            st.pop("finished", None)
            if timing:
                st.pop("anchor", None)
            self._plan(new, self.clock())
        if new.enabled and not s.enabled:
            st["failures_in_row"], st["paused_reason"] = 0, ""
        self._event("schedule_updated", new, f"Schedule {new.name} changed", by=by)
        self.save()
        self.poke()
        return self.view(new)

    def set_enabled(self, sid: str, on: bool, by: str = "user", reason: str = "") -> dict:
        s = self.get(sid)
        st = self.st(sid)
        s.enabled = on
        if on:
            st["failures_in_row"], st["paused_reason"] = 0, ""
            st.pop("finished", None)
            self._plan(s, self.clock())
        else:
            st["paused_reason"] = reason
        self._event("schedule_resumed" if on else "schedule_paused", s,
                    f"Schedule {s.name} {'resumed' if on else 'paused'}" + (f": {reason}" if reason else ""), by=by,
                    reason=reason)
        self.save()
        self.poke()
        return self.view(s)

    def delete(self, sid: str, by: str = "user") -> dict:
        s = self.get(sid)
        if s.source in LOCKED:
            raise PermissionError(f"{s.name} comes from agents.json: remove it there")
        self.schedules.pop(sid)
        self.state.pop(sid, None)
        folder = self.root / ".schedules" / sid
        if folder.is_dir():
            import shutil
            shutil.rmtree(folder, ignore_errors=True)
        self._event("schedule_deleted", s, f"Schedule {s.name} deleted", by=by)
        self.save()
        self.poke()
        return {"id": sid, "deleted": True}

    async def run_now(self, sid: str, by: str = "user") -> dict:
        """Start a run at once, whatever the timetable and the overlap policy say (the next run stays)."""
        s = self.get(sid)
        task = await self._fire(s, "manual", by)
        return {"schedule": self.view(s), "task_id": task.id if task else None}

    # ---- timing -----------------------------------------------------------
    def _plan(self, s: Schedule, now: float) -> None:
        st = self.st(s.id)
        anchor = st.setdefault("anchor", now)  # intervals count from when the schedule was made or retimed
        nxt = next_after(s, now, anchor) if s.enabled else None
        if nxt is not None and s.end.get("until") and nxt > s.end["until"]:
            nxt = None
        if nxt is None and s.enabled and st.get("next_run") is None and s.trigger["kind"] == "once" \
                and not st.get("last_run") and s.trigger["at"] <= now:
            nxt = s.trigger["at"]  # a one-off time already passed when it was saved: run it now
        st["next_run"] = nxt
        if nxt is None and s.enabled and s.trigger["kind"] != "feed":
            self._finish(s, "no more run times")

    def _finish(self, s: Schedule, why: str) -> None:
        st = self.st(s.id)
        if st.get("finished"):
            return
        st["finished"], st["next_run"] = True, None
        s.enabled = False
        st["paused_reason"] = why
        self._event("schedule_finished", s, f"Schedule {s.name} finished: {why}", reason=why)

    def _ended(self, s: Schedule) -> str:
        st = self.st(s.id)
        if s.end.get("after_runs") and st.get("runs", 0) >= s.end["after_runs"]:
            return f"{s.end['after_runs']} run(s) done"
        if s.trigger["kind"] == "once" and st.get("runs", 0) >= 1:
            return "one-off run done"
        return ""

    def startup(self) -> None:
        """At start: settle runs missed while the server was down, then plan the next ones."""
        now = self.clock()
        for s in self.schedules.values():
            st = self.st(s.id)
            if st.get("last_status") in ("running", "queued"):
                st["last_status"] = "interrupted"
            st["queued"] = False
            due = st.get("next_run")
            if not s.enabled or due is None or due > now:
                if s.enabled and due is None:
                    self._plan(s, now)
                continue
            if s.missed == "run_once" and now - due <= s.grace:
                st["next_run"] = now  # runs on the first tick
                st["catch_up"] = True
                continue
            self._event("schedule_missed", s, f"Schedule {s.name}: run due {iso(due)} was missed (server down)",
                        due=iso(due))
            self._plan(s, now)
        self.save()

    # ---- the loop -----------------------------------------------------------
    def poke(self) -> None:
        if self._wake:
            self._wake.set()

    def _take_lock(self) -> bool:
        try:
            import fcntl
        except ImportError:  # Windows: no lock; never run two servers on one data dir
            return True
        self.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.root / "scheduler.lock", os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._lock_fd = fd
        return True

    def start(self) -> bool:
        if self._loop_task:
            return True
        if not self._take_lock():
            self.lock_error = ("another xqo process is running the schedules of this data dir; "
                               "schedules do not run here")
            return False
        self.running = True
        self._wake = asyncio.Event()
        self.startup()
        self._loop_task = asyncio.get_running_loop().create_task(self._loop())
        return True

    async def stop(self) -> None:
        if self._loop_task:
            self._loop_task.cancel()
            with contextlib.suppress(BaseException):
                await self._loop_task
            self._loop_task = None
        self.running = False
        if self._lock_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._lock_fd)
            self._lock_fd = None

    async def _loop(self) -> None:
        while True:
            self._wake.clear()
            try:
                await self.tick()
            except Exception as e:  # never let one bad schedule stop the others
                self.bus.emit(Event("error", "scheduler", None, None, None, f"scheduler: {type(e).__name__}: {e}"))
            now = self.clock()
            dues = [self.st(s.id).get("next_run") for s in self.schedules.values() if s.enabled]
            dues = [d for d in dues if d is not None]
            delay = min([TICK] + [max(0.2, d - now) for d in dues])
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), delay)

    async def tick(self) -> None:
        if self.bus.on_hold():  # draining or a backup: due runs wait (and catch up afterwards)
            return
        now = self.clock()
        changed = False
        for s in list(self.schedules.values()):
            st = self.st(s.id)
            if st.get("queued") and not self._busy(s):
                st["queued"] = False
                changed = True
                await self._fire(s, "queued")
                continue
            due = st.get("next_run")
            if not s.enabled or due is None or due > now:
                continue
            reason = "missed" if st.pop("catch_up", False) else "schedule"
            changed = True
            if self._busy(s) and s.overlap != "parallel":
                if s.overlap == "queue":
                    if not st.get("queued"):
                        st["queued"] = True
                        st["last_status"] = "queued"
                        self._event("schedule_run", s, f"{s.name}: previous run still going; this run waits",
                                    outcome="queued")
                else:
                    self._event("schedule_run", s, f"{s.name}: skipped, the previous run is still going",
                                outcome="skipped", due=iso(due))
                self._plan(s, now)
                continue
            self._plan(s, max(now, due))
            await self._fire(s, reason)
        if now - self._last_archive >= ARCHIVE_CHECK:
            self._last_archive = now
            self.archive_old(now)
        if changed:
            self.save()

    def _busy(self, s: Schedule) -> bool:
        for tid in list(self._jobs.get(s.id, ())):
            t = self.bus.tasks.get(tid)
            if t and (t.status in ACTIVE or self.bus.is_running(tid)):
                return True
        return False

    # ---- firing ------------------------------------------------------------
    def _files(self, s: Schedule) -> list[dict]:
        out = []
        for m in s.attachments:
            p = self.files.path(s.id, m["name"])
            if p:
                out.append({"name": m["name"], "media_type": m["media_type"], "kind": m["kind"],
                            "data": p.read_bytes()})
        return out

    def register_feed(self, d: dict, meta: dict) -> Schedule:
        """A schedule for a feed rule (feeds/): it runs only when the rule fires, or from "Run now"."""
        s = normalize(d, "feed")
        self._check_agent(s)
        st = self.state.get(s.id) or {}
        if "enabled" in st:
            s.enabled = bool(st["enabled"])
        self.schedules[s.id] = s
        self.feed_meta[s.id] = dict(meta)
        return s

    async def fire_feed(self, sid: str, prompt: str, info: dict):
        """Start a run of a feed rule's schedule with the event (or digest) as the prompt."""
        s = self.get(sid)
        if not s.enabled:
            return None
        task = await self._fire(s, "feed", prompt=prompt, extra=info)
        self.save()
        return task

    async def _fire(self, s: Schedule, reason: str, by: str = "scheduler", prompt: str | None = None,
                    extra: dict | None = None):
        st = self.st(s.id)
        agent = s.agent if s.agent else None
        if agent and agent not in self.bus.agents:
            st["last_status"], st["last_run"] = "error", self.clock()
            self._event("schedule_run", s, f"{s.name}: agent {agent} does not exist (any more)", outcome="error")
            self._failed(s)
            self.save()
            return None
        n = st.get("runs", 0) + 1
        info = {"id": s.id, "name": s.name, "run": n, "reason": reason}
        if s.id in self.feed_meta:  # feeds/: the task tree runs in the rule's mode (report or act)
            info["feed"] = {**self.feed_meta[s.id], **(extra or {})}
        limits = {"honor_no_limits": False, **s.limits}
        task = await self.bus.create(prompt or s.prompt, agent, files=self._files(s) or None, title=f"{s.name} #{n}",
                                     meta={"schedule": info, "limits": limits, "unattended": s.unattended})
        st.update(runs=n, last_run=self.clock(), last_task=task.id, last_status="running")
        self._jobs.setdefault(s.id, set()).add(task.id)
        self._event("schedule_run", s, f"{s.name}: run {n} started ({reason})", task_id=task.id,
                    outcome="started", run=n, reason=reason, by=by)
        why = self._ended(s)
        if why:
            self._finish(s, why)
        self.save()
        self.bus.spawn(self._job(s.id, task))
        return task

    def tree_cost(self, root_id: str) -> float:
        """Estimated $ of a task tree so far: recorded usage plus model calls of sessions still open."""
        ids = self.bus.tree_ids(root_id)
        cost = sum(float((self.bus.tasks[t].usage or {}).get("cost_usd") or 0) for t in ids if t in self.bus.tasks)
        for a in self.bus.agents.values():
            for tid, sess in list((getattr(a, "_sessions", None) or {}).items()):
                if tid in ids:
                    cost += float((sess or {}).get("cost_usd") or 0)
        return cost

    async def _job(self, sid: str, task) -> None:
        cap = (task.limits or {}).get("max_cost_usd")
        run = self.bus.spawn(self.bus._run(task))
        capped = False
        try:
            while not run.done():
                await asyncio.wait({run}, timeout=COST_CHECK)
                if cap and not run.done() and not capped and self.tree_cost(task.id) >= cap:
                    capped = True
                    with contextlib.suppress(KeyError, ValueError):
                        await self.bus.cancel(task.id, "scheduler", f"cost cap ${cap:g} reached", wait=0)
            with contextlib.suppress(BaseException):
                await run
        finally:
            self._jobs.get(sid, set()).discard(task.id)
            self._done(sid, task, capped)

    def _done(self, sid: str, task, capped: bool) -> None:
        s = self.schedules.get(sid)
        if not s:
            return
        st = self.st(sid)
        status = "cost_cap" if capped else task.status
        if st.get("last_task") == task.id:
            st["last_status"] = status
        failed = capped or task.status == "error" or (task.status == "cancelled" and not self._cancelled_by_user(task))
        cost = self.tree_cost(task.id)
        self._event("schedule_run", s, f"{s.name}: run {(task.schedule or {}).get('run', '?')} {status}"
                    + (f" (${cost:.2f})" if cost else ""), task_id=task.id, outcome=status,
                    run=(task.schedule or {}).get("run"), cost_usd=round(cost, 4), failed=failed)
        if failed:
            self._failed(s)
        else:
            st["failures_in_row"] = 0
        self.save()
        self.poke()  # a queued run may start now

    def _cancelled_by_user(self, task) -> bool:
        return (task.result or "").startswith("Cancelled by user")

    def _failed(self, s: Schedule) -> None:
        st = self.st(s.id)
        st["failures_in_row"] = st.get("failures_in_row", 0) + 1
        if s.max_failures and st["failures_in_row"] >= s.max_failures and s.enabled:
            s.enabled = False
            st["next_run"] = None
            st["paused_reason"] = f"{st['failures_in_row']} failed runs in a row"
            self._event("schedule_paused", s, f"Schedule {s.name} paused after {st['failures_in_row']} failed "
                        "runs in a row", by="scheduler", reason=st["paused_reason"], alert=True)

    def archive_old(self, now: float | None = None) -> list[str]:
        """Archive finished runs older than their schedule's archive_after."""
        arch = getattr(self.bus, "lifecycle", None)
        if not arch:
            return []
        now = now or self.clock()
        done = []
        for t in list(self.bus.tasks.values()):
            sch = t.schedule or {}
            s = self.schedules.get(sch.get("id", ""))
            if t.id != t.root_id or not s or not s.archive_after or t.archived or t.status in ACTIVE \
                    or self.bus.is_running(t.id):
                continue
            if now - parse_time(t.created, None) >= s.archive_after:
                with contextlib.suppress(KeyError, ValueError):
                    arch.archive(t.id, "scheduler")
                    done.append(t.id)
        return done

    def _event(self, kind: str, s: Schedule, text: str, task_id: str | None = None, by: str = "scheduler",
               **meta) -> None:
        self.bus.emit(Event(kind, by, s.agent, task_id, None, text,
                            {"schedule_id": s.id, "schedule_name": s.name, **meta}))
