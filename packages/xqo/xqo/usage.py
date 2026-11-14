"""Overall token usage and estimated cost of this xqo, shown in the office header (GET /api/usage).

Kept in <artifacts>/usage.json, so the total survives restarts and does not drop when tasks are deleted. The first
time (no file yet) it starts from the usage of the tasks the server still knows. Every finished agent run adds its
session (bus.record_usage), so a task that is still running is counted when its runs end.

It also keeps the last plan limit state Claude Code reported (RateLimitEvent: five-hour / weekly window, how much
of it is used, when it resets), one entry per window, for the plan-limit chip in the office and dashboard headers.
Claude Code sends it only when the state changes, and never for API-key logins, so it is often empty.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .models import utcnow

KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
LIMIT_STATUS = ("allowed", "allowed_warning", "rejected")


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def rate_limit_entry(info) -> dict | None:
    """A RateLimitInfo (SDK dataclass or its dict) as a small JSON dict, or None when it says nothing usable."""
    g = (lambda k: info.get(k)) if isinstance(info, dict) else (lambda k: getattr(info, k, None))
    status = g("status")
    if status not in LIMIT_STATUS:
        return None
    util = _num(g("utilization"))
    return {"type": str(g("rate_limit_type") or "plan"), "status": status,
            "utilization": None if util is None else max(0.0, min(1.0, float(util))),
            "resets_at": _num(g("resets_at")), "overage_status": g("overage_status") if g("overage_status") in
            LIMIT_STATUS else None, "ts": utcnow()}


class UsageTotals:
    def __init__(self, root: str | os.PathLike, tasks=()):
        self.path = Path(root) / "usage.json"
        self.data = self._load()
        if self.data is None:
            self.data = {"since": utcnow(), "cost_usd": 0.0, "tokens": 0, "sessions": 0, **{k: 0 for k in KEYS}}
            for t in tasks:
                if t.usage:
                    self._add(t.usage, t.usage.get("sessions") or 1)
            self._save()

    def _load(self) -> dict | None:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):  # a broken file: keep it aside and count again
            try:
                self.path.replace(self.path.with_suffix(".json.bad"))
            except OSError:
                pass
            return None
        if not isinstance(d, dict):
            return None
        out = {"since": str(d.get("since") or utcnow())}
        rl = d.get("rate_limits")
        if isinstance(rl, dict):
            out["rate_limits"] = {k: v for k, v in rl.items() if isinstance(v, dict) and v.get("status") in LIMIT_STATUS}
        for k in ("cost_usd", "tokens", "sessions", *KEYS):
            v = d.get(k)
            out[k] = v if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else 0
        return out

    def _add(self, usage: dict, sessions: int = 1) -> None:
        g = lambda k: usage.get(k) if isinstance(usage.get(k), (int, float)) and not isinstance(usage.get(k), bool) else 0
        for k in KEYS:
            self.data[k] += g(k)
        self.data["tokens"] += sum(g(k) for k in KEYS)
        self.data["cost_usd"] = round(self.data["cost_usd"] + g("cost_usd"), 6)
        self.data["sessions"] += sessions

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.data), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:  # the total is cosmetic: never fail a task over it
            pass

    def add(self, session: dict) -> None:
        """One finished run's usage (the session dict bus.record_usage gets)."""
        self._add(session or {})
        self._save()

    def rate_limit(self, info) -> dict | None:
        """Keep the newest state of one plan limit window; returns the stored entry (None when ignored)."""
        e = rate_limit_entry(info)
        if e is None:
            return None
        self.data.setdefault("rate_limits", {})[e["type"]] = e
        self._save()
        return e

    def limits(self, now: float | None = None) -> list[dict]:
        """The plan limit windows that have not reset yet, the tightest first."""
        now = time.time() if now is None else now
        live = [v for v in (self.data.get("rate_limits") or {}).values() if not (v.get("resets_at") and v["resets_at"] < now)]
        rank = {s: i for i, s in enumerate(LIMIT_STATUS)}
        return sorted(live, key=lambda v: (-rank[v["status"]], -(v.get("utilization") or 0)))

    def report(self) -> dict:
        out = {k: v for k, v in self.data.items() if k != "rate_limits"}
        out["rate_limits"] = self.limits()
        return out
