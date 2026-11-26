"""Triggers: schedules and webhooks that start flow runs without a person (the flow's "triggers" list, model.py).

Schedules use the cron / every / at parser of xqo-schedules (install it for schedule triggers; without it a
schedule trigger shows a warning and never fires). Their state (next run, last run) is in
artifacts/flows/triggers.json. A run missed while the server was down runs once at start-up; older misses are not
piled up. With overlap "skip" (default) a run is skipped while the trigger's previous run is still going.

Webhooks: POST /flows/v1/hooks/<flow>/<trigger id> on the public listener ("flows.listen"), or
/api/flows/hooks/<flow>/<trigger id> on the dashboard's own address. The caller proves it knows the secret
(read from the environment variable named by secret_env) with either
    Authorization: Bearer <secret>
    X-Hub-Signature-256: sha256=<HMAC-SHA256 of the body>   (GitHub's header; X-XQO-Signature works the same)
Inputs may take values from the request: {{body}} (the raw body), {{body.a.b}} (a field of a JSON body, lists by
index), {{headers.x-github-event}}. Unless the trigger says "trust": "trusted", those values reach the prompts
inside <untrusted_webhook_data> markers, so agents treat them as data, not instructions."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .model import FlowError

try:  # the cron / every / at parser of xqo-schedules
    from xqo_schedules.scheduler import Cron, parse_duration, parse_time, zone
    HAVE_SCHEDULES = True
except ImportError:  # pragma: no cover - depends on what is installed
    HAVE_SCHEDULES = False

FIELD_RE = re.compile(r"\{\{\s*(body|headers)((?:\.[A-Za-z0-9_\-]+)*)\s*\}\}")
MAX_HOOK_BODY = 1_000_000
GRACE = 3600.0  # a run missed by more than this while the server was down is skipped


def iso(e: float | None) -> str | None:
    """Epoch seconds as an ISO 8601 UTC string (None stays None)."""
    if e is None:
        return None
    return datetime.fromtimestamp(e, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def next_time(t: dict, after: float, anchor: float | None = None) -> float | None:
    """The trigger's next fire time strictly after `after` (epoch seconds), None if it never fires again."""
    if not HAVE_SCHEDULES:
        raise FlowError("schedule triggers need the xqo-schedules package (pip install xqo-schedules)")
    tz = zone(t.get("tz"))
    if t.get("cron"):
        return Cron(t["cron"]).next_after(after, tz)
    if t.get("every"):
        step = parse_duration(t["every"], "every")
        base = anchor if anchor is not None else after
        n = max(1, int((after - base) // step) + 1)
        return base + n * step
    at = parse_time(t["at"], tz)
    return at if at > after else None


def check_schedule(t: dict) -> str:
    """'' when the schedule can fire, else why not (shown as a warning when the flow is saved)."""
    try:
        if next_time(t, time.time()) is None:
            return f"trigger {t['id']}: it never fires again"
    except (FlowError, ValueError) as e:
        return f"trigger {t['id']}: {e}"
    return ""


class TriggerState:
    """next_run / last_run per trigger, keyed "<flow>/<trigger id>", with a hash of the trigger's settings so an
    edited trigger is planned again."""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.d: dict = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, ValueError):
            self.d = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.d, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def get(self, key: str) -> dict:
        return self.d.setdefault(key, {})


def fingerprint(t: dict) -> str:
    keys = {k: t.get(k) for k in ("type", "cron", "every", "at", "tz")}
    return hashlib.sha1(json.dumps(keys, sort_keys=True).encode()).hexdigest()[:12]


# ---- webhooks ----------------------------------------------------------------------------------------------
def verify(t: dict, body: bytes, headers: dict[str, str]) -> None:
    """Raise PermissionError unless the request carries the trigger's secret or a valid signature."""
    secret = os.getenv(t["secret_env"], "")
    if not secret:
        raise PermissionError(f"the webhook's secret is not set on this HQ ({t['secret_env']})")
    auth = headers.get("authorization", "")
    if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].strip().encode(), secret.encode()):
        return
    sig = headers.get("x-hub-signature-256") or headers.get("x-xqo-signature") or ""
    if sig.startswith("sha256="):
        want = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig[7:].strip().lower().encode(), want.encode()):
            return
    raise PermissionError("missing or wrong webhook secret / signature")


def _dig(value, path: list[str]):
    for p in path:
        if isinstance(value, dict):
            value = value.get(p)
        elif isinstance(value, list) and p.isdigit() and int(p) < len(value):
            value = value[int(p)]
        else:
            return ""
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def hook_inputs(t: dict, body: bytes, headers: dict[str, str]) -> dict[str, str]:
    """The run's inputs from the trigger's templates and the request."""
    text = body.decode("utf-8", "replace")
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError:
        data = None
    wrap = t.get("trust", "untrusted") != "trusted"

    def one(m: re.Match) -> str:
        kind, path = m.group(1), [p for p in m.group(2).split(".") if p]
        if kind == "headers":
            val = headers.get(path[0].lower(), "") if path else ""
        elif not path:
            val = text
        else:
            val = _dig(data, path) if data is not None else ""
        val = str(val)[:100_000]
        return f"<untrusted_webhook_data>\n{val}\n</untrusted_webhook_data>" if wrap and val else val

    return {k: FIELD_RE.sub(one, v) for k, v in (t.get("inputs") or {}).items()}


class RateLimit:
    def __init__(self):
        self.hits: dict[str, list[float]] = {}

    def allow(self, key: str, per_minute: int) -> bool:
        now = time.monotonic()
        hits = [h for h in self.hits.get(key, []) if now - h < 60]
        if len(hits) >= per_minute:
            self.hits[key] = hits
            return False
        hits.append(now)
        self.hits[key] = hits
        return True
