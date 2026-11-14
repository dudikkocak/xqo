"""Every limit in one place.

1. Sizes ("limits" in agents.json, environment variables win): workspace file, attachment sizes and counts.
   `Limits.from_config` checks them and applies them to artifacts.py / attachments.py for this process.
2. The system-wide "no limits" override: for one hour every agent runs without its max_turns limit, with the
largest output limit (UNLIMITED_MAX_TOKENS instead of max_tokens), without the per-task time limit
(bus.TASK_TIMEOUT) and without department token / cost budgets, tasks already running included: agents
read the override before every model call. Turning it on answers open budget / time-limit questions with
'extend'. When the hour is over every task gets a fresh time period and a fresh budget from where it stands,
so nothing is stopped the moment the override ends.

Turning it on again at any time starts a fresh hour; it reverts by itself when the hour is over. The expiry is
kept in <artifacts>/limits_override.json so a restart keeps it. A scheduled run ignores the override unless its
limits say honor_no_limits; its own max_cost_usd cap always stays in force."""
from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

OVERRIDE_SECONDS = 3600
UNLIMITED_MAX_TOKENS = int(os.getenv("XQO_UNLIMITED_MAX_TOKENS", "128000"))  # output limit while the override is on

# "limits" in agents.json: setting -> (default, environment variable that wins); sizes in decimal MB
LIMITS = {
    "workspace_file_mb": (5, "XQO_WORKSPACE_FILE_MB"),          # one text file an agent writes or patches
    "image_mb": (5, "XQO_IMAGE_MB"),                            # one image attachment
    "pdf_mb": (20, "XQO_PDF_MB"),                               # one PDF attachment
    "text_attachment_mb": (2, "XQO_TEXT_ATTACHMENT_MB"),        # one text attachment
    "attachments_total_mb": (24, "XQO_ATTACHMENTS_TOTAL_MB"),   # all attachments of one task / message
    "attachments_max_files": (20, "XQO_ATTACHMENTS_MAX_FILES"),  # attachments per task / message
}
API_SAFE = {"image_mb": 5, "pdf_mb": 32, "attachments_total_mb": 24}  # above these, model calls may be refused

def apply_limits(d: dict | None) -> tuple[dict, list[str]]:
    """Set the workspace and attachment limits for this process. Returns (values, warnings)."""
    from . import artifacts, attachments  # noqa: PLC0415 (they import nothing from here)
    d = dict(d or {})
    bad = set(d) - set(LIMITS)
    if bad:
        raise ValueError(f"limits: unknown setting(s) {', '.join(sorted(bad))}; known: {', '.join(LIMITS)}")
    v = {}
    for k, (default, env) in LIMITS.items():
        raw = os.getenv(env, d.get(k, default))
        try:
            v[k] = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"limits.{k} must be a number, got {raw!r}") from None
        if v[k] <= 0:
            raise ValueError(f"limits.{k} must be positive")
        v[k] = int(v[k]) if v[k] == int(v[k]) else v[k]
    mb = lambda k: int(v[k] * 1_000_000)  # noqa: E731
    artifacts.MAX_FILE_BYTES = mb("workspace_file_mb")
    attachments.MAX_BYTES = {"image": mb("image_mb"), "pdf": mb("pdf_mb"), "text": mb("text_attachment_mb")}
    attachments.MAX_TOTAL = mb("attachments_total_mb")
    attachments.MAX_FILES = int(v["attachments_max_files"])
    warnings = [f"limits.{k} = {v[k]} MB is above what the Claude API accepts ({cap} MB): model calls with such "
                "attachments may be refused" for k, cap in API_SAFE.items() if v[k] > cap]
    return v, warnings

class Limits:
    """Size limits (`config`, `warnings`) plus the no-limits hour (`active`, `enable`, ...). Lives at bus.limits."""

    def __init__(self, path: str | Path | None = None, clock=time.time, config: dict | None = None,
                 warnings: list[str] | None = None):
        self.config = config or {}
        self.warnings = warnings or []
        self.clock = clock
        self.until = 0.0  # epoch seconds; the override is on while clock() < until
        self.by = ""
        self.path = None
        self.persist(path)

    @classmethod
    def from_config(cls, d: dict | None, path: str | Path | None = None, clock=time.time) -> "Limits":
        """Check and apply the "limits" block (raises ValueError on a mistake)."""
        values, warnings = apply_limits(d)
        return cls(path, clock, values, warnings)

    def persist(self, path: str | Path | None) -> "Limits":
        """Keep the override in this file (so a restart keeps it) and load what is there."""
        self.path = Path(path) if path else None
        if self.path and self.path.is_file():
            try:
                d = json.loads(self.path.read_text(encoding="utf-8"))
                self.until, self.by = float(d.get("until") or 0), str(d.get("by") or "")
            except (ValueError, OSError):
                pass
        return self

    def active(self) -> bool:
        return self.clock() < self.until

    def enable(self, by: str = "user", seconds: float = OVERRIDE_SECONDS) -> dict:
        """Turn the override on for a fresh hour (also when it is already on)."""
        self.until, self.by = self.clock() + seconds, by
        self._save()
        return self.status()

    def disable(self) -> dict:
        self.until, self.by = 0.0, ""
        self._save()
        return self.status()

    def lifts(self, task_limits: dict | None = None) -> bool:
        """True while the override is on and applies to a task tree with these limits (scheduler.py)."""
        return self.active() and (task_limits or {}).get("honor_no_limits", True)

    def max_turns(self, limit: int) -> float:
        return math.inf if self.active() else limit

    def max_tokens(self, limit: int) -> int:
        return max(limit, UNLIMITED_MAX_TOKENS) if self.active() else limit

    def status(self) -> dict:
        on = self.active()
        return {"active": on, "until": self.until if on else None,
                "remaining": max(0, round(self.until - self.clock())) if on else 0,
                "by": self.by if on else "", "duration": OVERRIDE_SECONDS, "max_tokens": UNLIMITED_MAX_TOKENS,
                "covers": ["max_turns", "max_tokens", "budget", "time"]}

    def describe(self) -> dict:
        return {"sizes": self.config, "override": self.status()}

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"until": self.until, "by": self.by}), encoding="utf-8")
        os.replace(tmp, self.path)
