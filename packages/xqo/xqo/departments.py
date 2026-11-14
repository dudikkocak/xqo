"""Departments: groups of agents with a head, contact rules, own resources and an optional budget.

One JSON file per department in a ``departments/`` folder:
    {"name": "engineering", "description": "...", "head": "eng_lead",
     "contacts": ["research"],                 # departments it may reach ("*" = all; HQ is always allowed)
     "budget": {"max_tokens": 2000000, "max_cost_usd": 10},   # per task tree, optional
     "sandbox": {"network": false},            # overrides the global sandbox for its members
     "mcp_servers": [...],                     # servers only its members can be granted
     "default_model": "claude-sonnet-5-5",     # optional: model for members that don't set "model"
     "default_effort": "medium",               # optional, likewise for "effort"
     "default_provider": "ollama",             # optional, likewise for "provider" (name or OpenAI-compatible URL)
     "default_max_tokens": 32000,              # optional, likewise for "max_tokens" (output limit per call)
     "agents": [{"name": "eng_lead", "skills": [...], "system": "...", "model": "..."}, ...]}
Agents listed in agents.json itself form headquarters ("hq"): they can reach every department.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .models import Event

HQ = "hq"
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
PACKAGED_DEPARTMENTS = Path(__file__).parent / "default_departments"

# $ per million tokens (Anthropic first-party rates, 2026-10). Override with "pricing" in agents.json.
DEFAULT_PRICING = {
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_read": 0.20, "cache_write": 5.0},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_read": 0.20, "cache_write": 2.5},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125},
    "claude-fable-5-1": {"input": 10.0, "output": 50.0, "cache_read": 0.25, "cache_write": 12.5},
}


def dept_root(root_id: str, dept: str) -> str:
    """Storage key for a department's own workspace / notes inside one task tree."""
    return f"{root_id}.{dept}"


@dataclass
class Department:
    name: str
    description: str = ""
    head: str | None = None
    members: list[str] = field(default_factory=list)
    contacts: list[str] | str = "*"
    budget: dict = field(default_factory=dict)
    sandbox: dict | None = None
    mcp_servers: list[dict] = field(default_factory=list)
    source: str = ""

    def may_contact(self, other: str) -> bool:
        return other in (self.name, HQ) or self.contacts == "*" or other in self.contacts

    def to_dict(self) -> dict:
        return {"name": self.name, "description": self.description, "head": self.head, "members": self.members,
                "contacts": self.contacts, "budget": self.budget, "source": self.source,
                "mcp": [s["name"] for s in self.mcp_servers], "sandbox": self.sandbox}


def resolve_dir(config_path: str | Path, cfg: dict) -> Path | None:
    """Where department files live: XQO_DEPARTMENTS_DIR, "departments_dir" in agents.json,
    a departments/ folder next to agents.json, else the packaged examples."""
    if os.getenv("XQO_DEPARTMENTS_DIR"):
        return Path(os.environ["XQO_DEPARTMENTS_DIR"])
    base = Path(config_path).parent
    if cfg.get("departments_dir"):
        return (base / cfg["departments_dir"]).resolve()
    local = base / "departments"
    return local if local.is_dir() else PACKAGED_DEPARTMENTS


def parse_only(value: str | list | None) -> list[str] | None:
    """--departments eng,marketing -> ['eng', 'marketing']; None = all; 'none' = no departments."""
    if value is None:
        value = os.getenv("XQO_DEPARTMENTS")
    if value is None or value == "" or value == "all":
        return None
    items = value if isinstance(value, list) else value.split(",")
    items = [s.strip() for s in items if s.strip()]
    return [] if items == ["none"] else items


def load_files(folder: Path | None, only: list[str] | None = None) -> list[dict]:
    """Read department files; `only` filters by department name or file stem."""
    if folder is None or not folder.is_dir():
        if only:
            raise ValueError(f"departments folder {folder} not found")
        return []
    out, seen = [], set()
    for p in sorted(folder.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            if only is not None and p.stem not in only:
                continue  # not selected: a broken file shouldn't block the others
            raise ValueError(f"{p.name}: invalid JSON ({e})") from None
        d.setdefault("name", p.stem)
        d["_source"] = str(p)
        if only is not None and d["name"] not in only and p.stem not in only:
            continue
        name = d["name"]
        if not NAME_RE.match(name) or name == HQ:
            raise ValueError(f"{p.name}: invalid department name {name!r}")
        if name in seen:
            raise ValueError(f"{p.name}: duplicate department {name!r}")
        seen.add(name)
        agents = d.get("agents") or []
        if not agents:
            raise ValueError(f"{p.name}: department {name!r} has no agents")
        names = [a["name"] for a in agents]
        head = d.get("head") or names[0]
        if head not in names:
            raise ValueError(f"{p.name}: head {head!r} is not one of its agents {names}")
        d["head"] = head
        out.append(d)
    if only:
        missing = set(only) - {d["name"] for d in out} - {Path(d["_source"]).stem for d in out}
        if missing:
            raise ValueError(f"unknown department(s): {', '.join(sorted(missing))}")
    return out


def make_department(d: dict) -> Department:
    return Department(name=d["name"], description=d.get("description", ""), head=d["head"],
                      members=[a["name"] for a in d["agents"]], contacts=d.get("contacts", "*"),
                      budget=d.get("budget") or {}, sandbox=d.get("sandbox"),
                      mcp_servers=d.get("mcp_servers") or [], source=d.get("_source", ""))


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    """Token and cost accounting per department, per task tree, with optional caps.

    When a cap is reached the user is flagged ('extend' adds another budget's worth, default 'stop')."""

    def __init__(self, bus, pricing: dict | None = None):
        self.bus = bus
        self.pricing = {**DEFAULT_PRICING, **(pricing or {})}
        self.used: dict[tuple[str, str], dict] = {}  # (root, dept) -> {tokens, cost}
        self.totals: dict[str, dict] = {}  # dept -> {tokens, cost, calls}
        self.extensions: dict[tuple[str, str], int] = {}
        self.offsets: dict[tuple[str, str], dict] = {}  # extra cap after a "no limits" hour: a fresh budget
        self.lifted: set[tuple[str, str]] = set()  # over budget while "no limits" was on
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    def cost(self, model: str, usage) -> float:
        p = self.pricing.get(model) or next((v for k, v in self.pricing.items() if model.startswith(k)), None)
        if not p:
            return 0.0
        g = lambda k: (getattr(usage, k, None) if not isinstance(usage, dict) else usage.get(k)) or 0
        return (g("input_tokens") * p["input"] + g("output_tokens") * p["output"]
                + g("cache_read_input_tokens") * p.get("cache_read", p["input"] * 0.1)
                + g("cache_creation_input_tokens") * p.get("cache_write", p["input"] * 1.25)) / 1e6

    def add(self, root_id: str, dept: str, model: str, usage) -> dict:
        g = lambda k: (getattr(usage, k, None) if not isinstance(usage, dict) else usage.get(k)) or 0
        tokens = sum(g(k) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                    "cache_creation_input_tokens"))
        c = self.cost(model, usage)
        u = self.used.setdefault((root_id, dept), {"tokens": 0, "cost": 0.0})
        u["tokens"] += tokens
        u["cost"] += c
        t = self.totals.setdefault(dept, {"tokens": 0, "cost": 0.0, "calls": 0})
        t["tokens"] += tokens
        t["cost"] += c
        t["calls"] += 1
        return u

    def caps(self, root_id: str, dept: str) -> dict:
        d = self.bus.departments.get(dept)
        b = (d.budget if d else {}) or {}
        k = 1 + self.extensions.get((root_id, dept), 0)
        off = self.offsets.get((root_id, dept), {})
        return {key: b[key] * k + off.get(key, 0) for key in ("max_tokens", "max_cost_usd") if b.get(key)}

    def _fresh(self, key: tuple[str, str]) -> None:
        """The "no limits" hour is over: the cap becomes what is used now plus one more budget."""
        d = self.bus.departments.get(key[1])
        b = (d.budget if d else {}) or {}
        u = self.used.get(key, {"tokens": 0, "cost": 0.0})
        base = {k: b[k] * (1 + self.extensions.get(key, 0)) for k in ("max_tokens", "max_cost_usd") if b.get(k)}
        used = {"max_tokens": u["tokens"], "max_cost_usd": u["cost"]}
        self.offsets[key] = {k: max(0, used[k] + b[k] - v) for k, v in base.items()}

    def over(self, root_id: str, dept: str) -> str | None:
        caps, u = self.caps(root_id, dept), self.used.get((root_id, dept), {"tokens": 0, "cost": 0.0})
        if caps.get("max_tokens") and u["tokens"] >= caps["max_tokens"]:
            return f"{u['tokens']:,} tokens of {int(caps['max_tokens']):,}"
        if caps.get("max_cost_usd") and u["cost"] >= caps["max_cost_usd"]:
            return f"${u['cost']:.2f} of ${caps['max_cost_usd']:.2f}"
        return None

    async def check(self, task, agent: str, dept: str) -> None:
        """Before a model call: if the department is over budget for this task, ask the user."""
        key = (task.root_id, dept)
        lifted = bool(getattr(self.bus, "limits_lifted", None)) and self.bus.limits_lifted(task)
        if not lifted and key in self.lifted:  # the "no limits" hour ended: start a fresh budget
            self.lifted.discard(key)
            self._fresh(key)
        status = self.over(*key)
        if not status:
            return
        if lifted:  # "no limits": keep counting, don't ask
            if key not in self.lifted:
                self.lifted.add(key)
                self.bus.emit(Event("limits", agent, None, task.id, task.parent_id,
                                    f"Department '{dept}' passed its budget ({status}); no limits is on, "
                                    "so the work goes on", {"budget": status, "dept": dept, "no_limits": True}))
            return
        async with self._locks.setdefault(key, asyncio.Lock()):
            status = self.over(*key)  # another agent may have been granted an extension meanwhile
            if not status:
                return
            answer, auto = await self.bus.attention.request(
                task, agent, "budget",
                f"Department '{dept}' has used {status} of its budget on {task.root_id}. "
                f"Extend by another budget or stop its work?", default="stop", options=["extend", "stop"])
            if answer.strip().lower() in ("extend", "yes", "y", "continue"):
                self.extensions[key] = self.extensions.get(key, 0) + 1
                return
            raise BudgetExceeded(f"department '{dept}' budget exhausted ({status})"
                                 + (" - auto-confirmed stop" if auto else ""))

    def report(self, dept: str) -> dict:
        t = self.totals.get(dept, {"tokens": 0, "cost": 0.0, "calls": 0})
        return {**t, "cost": round(t["cost"], 4),
                "by_task": {r: {"tokens": u["tokens"], "cost": round(u["cost"], 4)}
                            for (r, d), u in self.used.items() if d == dept}}
