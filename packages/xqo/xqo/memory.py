"""Shared notes board per task tree: agents store facts and decisions instead of passing them in messages."""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from .models import utcnow

SAFE = re.compile(r"[^A-Za-z0-9_.-]+")
MAX_NOTES = 200
MAX_VALUE = 4000


class MemoryBoard:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._lock = threading.Lock()

    def _file(self, root_id: str) -> Path:
        return self.root / SAFE.sub("_", root_id) / "memory.json"

    def _load(self, root_id: str) -> dict:
        p = self._file(root_id)
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {"notes": {}, "log": []}

    def notes(self, root_id: str) -> dict[str, dict]:
        return self._load(root_id)["notes"]

    def log(self, root_id: str) -> list[dict]:
        return self._load(root_id)["log"]

    def set(self, root_id: str, key: str, value: str, author: str) -> dict:
        key = key.strip()[:80]
        if not key:
            raise ValueError("note key is empty")
        with self._lock:
            data = self._load(root_id)
            entry = {"key": key, "value": value[:MAX_VALUE], "author": author, "ts": utcnow()}
            if value.strip():
                if key not in data["notes"] and len(data["notes"]) >= MAX_NOTES:
                    raise ValueError(f"board is full ({MAX_NOTES} notes); delete or reuse a key")
                data["notes"][key] = entry
                entry["op"] = "set"
            else:
                if key not in data["notes"]:
                    raise ValueError(f"no note named {key!r}")
                data["notes"].pop(key)
                entry["op"] = "delete"
            data["log"].append(entry)
            p = self._file(root_id)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(data, indent=2), encoding="utf-8")
            return entry

    def restore(self, root_id: str, entries: list[dict]) -> dict[str, dict]:
        """Rebuild a board by replaying log entries (used when forking a task)."""
        data = {"notes": {}, "log": []}
        for e in entries:
            if e.get("op") == "delete":
                data["notes"].pop(e["key"], None)
            else:
                data["notes"][e["key"]] = {k: v for k, v in e.items() if k != "op"}
            data["log"].append(e)
        with self._lock:
            p = self._file(root_id)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return data["notes"]

    def render(self, root_id: str) -> str:
        notes = self.notes(root_id)
        if not notes:
            return ""
        lines = [f"- {n['key']}: {n['value']}  (by {n['author']})" for n in notes.values()]
        return "Shared notes board for this task (use note/read_notes to update):\n" + "\n".join(lines)
