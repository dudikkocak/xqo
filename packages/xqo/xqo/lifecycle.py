"""Task lifecycle: one store for what happens to a finished task tree after it is done (archive and trash).

States of a top-level task: live -> archived (hidden from the task list, keeps everything, never expires)
                                  -> trashed (folders moved to <artifacts>/.trash/<task>/, hidden, undo possible)
                                  -> purged (gone; a ``task_deleted`` record stays in the event log)
Both flags live in one file, <artifacts>/lifecycle.json: {"archived": {root: {at, by, name}}, "trashed": {root:
entry}}. (Before xqo these were archive.json and one trash.json per trashed folder.)

- Only finished trees (done, error, cancelled, interrupted) can be archived or deleted.
- Asking, steering or resuming an archived task unarchives it first; a fork starts unarchived.
- An archived task can still be deleted; restoring it from the trash brings it back archived.
- The trash is purged after "trash": {"retention_days": 7} (XQO_TRASH_DAYS; 0 = keep until purged by hand), or at
  once with a permanent delete, which also removes the tree's events from the store (store.py).
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .bus import ACTIVE
from .models import Event, utcnow

if TYPE_CHECKING:
    from .bus import MessageBus

DEFAULT_RETENTION_DAYS = 7.0
DELETABLE = {"done", "error", "cancelled", "interrupted"}
KINDS = {"task_archived", "task_unarchived"}


class TaskLifecycle:
    def __init__(self, bus: MessageBus, retention_days: float | None = None):
        self.bus = bus
        self.store = bus.artifacts
        env = os.getenv("XQO_TRASH_DAYS")
        self.retention_days = float(env) if env else (DEFAULT_RETENTION_DAYS if retention_days is None
                                                      else float(retention_days))
        self.dir = self.store.root / ".trash"
        self.path = self.store.root / "lifecycle.json"
        self.archived: dict[str, dict] = {}  # root id -> {at, by, name}
        self.trashed: dict[str, dict] = {}   # root id -> trash entry
        if self.path.is_file():
            try:
                d = json.loads(self.path.read_text(encoding="utf-8"))
                self.archived = dict(d.get("archived") or {})
                self.trashed = dict(d.get("trashed") or {})
            except (OSError, ValueError, AttributeError):
                pass
        for e in self.trash_entries():  # trashed before a restart: keep them hidden
            bus.hide(e["root_id"], set(e.get("ids") or []))
        for rid in self.archived:
            self.apply(rid)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"archived": self.archived, "trashed": self.trashed}, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def state(self, root_id: str) -> str:
        return "trashed" if root_id in self.trashed else "archived" if root_id in self.archived else "live"

    # ---- archive ---------------------------------------------------------
    def is_archived(self, root_id: str | None) -> bool:
        return bool(root_id) and root_id in self.archived

    def apply(self, root_id: str) -> None:
        """Mark a (re)loaded task record as archived."""
        t = self.bus.tasks.get(root_id)
        if t:
            t.archived = root_id in self.archived

    def archived_entries(self) -> list[dict]:
        out = []
        for rid, e in self.archived.items():
            t = self.bus.tasks.get(rid)
            if t:  # left out while the task is in the trash
                out.append({"root_id": rid, **e, "name": t.name, "status": t.status, "created": t.created,
                            "schedule": t.schedule})
        return sorted(out, key=lambda e: e["at"], reverse=True)

    def _archive_root(self, root_id: str):
        t = self.bus.tasks.get(root_id)
        if t is None:
            raise KeyError(f"unknown task {root_id}")
        if t.root_id != root_id:
            raise ValueError(f"{t.name} is a sub-task; archive its top-level task {t.root_id}")
        return t

    def archive(self, root_id: str, by: str = "user") -> dict:
        t = self._archive_root(root_id)
        if self.bus.is_running(root_id) or t.status in ACTIVE or t.status == "paused":
            raise ValueError(f"{t.name} is not finished; only finished tasks can be archived")
        if root_id in self.archived:
            return {"id": root_id, "name": t.name, "archived": True}
        self.archived[root_id] = {"at": utcnow(), "by": by, "name": t.name}
        t.archived = True
        self._save()
        self.bus.emit(Event("task_archived", by, None, root_id, None, f"{t.name} archived", {"name": t.name}))
        return {"id": root_id, "name": t.name, "archived": True}

    def unarchive(self, root_id: str, by: str = "user") -> dict:
        t = self._archive_root(root_id)
        if root_id not in self.archived:
            return {"id": root_id, "name": t.name, "archived": False}
        self.archived.pop(root_id)
        t.archived = False
        self._save()
        self.bus.emit(Event("task_unarchived", by, None, root_id, None, f"{t.name} unarchived", {"name": t.name}))
        return {"id": root_id, "name": t.name, "archived": False}

    def forget(self, root_id: str) -> None:
        """A task was deleted for good: drop its archive flag."""
        if self.archived.pop(root_id, None) is not None:
            self._save()

    # ---- trash: helpers --------------------------------------------------------
    @staticmethod
    def owns(root_id: str, key: str | None) -> bool:
        """True for the task's own storage key and its sub-keys (e.g. '<task>.<department>')."""
        return bool(key) and (key == root_id or key.startswith(root_id + "."))

    def folders(self, root_id: str) -> list[Path]:
        root = self.store.root
        return [p for p in [root / root_id, *sorted(root.glob(f"{root_id}.*"))] if p.is_dir()]

    def _entry_dir(self, root_id: str) -> Path:
        return self.dir / root_id

    def trash_entries(self) -> list[dict]:
        out = []
        for e in self.trashed.values():
            e = dict(e)
            if self.retention_days > 0:
                e["purge_at"] = e["deleted_epoch"] + self.retention_days * 86400
            out.append(e)
        return sorted(out, key=lambda e: e["deleted_epoch"], reverse=True)

    def entry(self, root_id: str) -> dict | None:
        """The trash entry of a deleted task (None if it is not in the trash)."""
        return next((e for e in self.trash_entries() if e["root_id"] == root_id), None)

    def branches(self, root_id: str) -> list[dict]:
        """Live tasks that were forked from this one (they are kept; only their link breaks)."""
        return [{"id": t.id, "name": t.name} for t in self.bus.tasks.values()
                if t.id == t.root_id and (t.forked_from or {}).get("task_id") == root_id]

    def check(self, root_id: str) -> None:
        t = self.bus.tasks.get(root_id)
        if t is None:
            raise KeyError(f"unknown task {root_id}")
        if t.root_id != root_id:
            raise ValueError(f"{t.name} is a sub-task; delete its top-level task {t.root_id}")
        if self.bus.is_running(root_id) or t.status in ACTIVE:
            raise ValueError(f"{t.name} is still running; cancel it first")

    # ---- soft delete / undo ---------------------------------------------
    def delete(self, root_id: str, by: str = "user", permanent: bool = False) -> dict:
        """Move a finished task to the trash (or purge it right away with `permanent`)."""
        self.check(root_id)
        t = self.bus.tasks[root_id]
        branches = self.branches(root_id)
        if permanent:
            ids = self.bus.tree_ids(root_id)
            self.bus.hide(root_id, ids)
            return self._purge(root_id, ids, t.name, by, branches)
        dst = self._entry_dir(root_id)
        dst.mkdir(parents=True, exist_ok=True)
        moved = []
        for f in self.folders(root_id):
            for env in (".venv", ".venv-docker", ".deps", ".deps-docker"):  # caches: rebuilt on the next run
                shutil.rmtree(f / env, ignore_errors=True)
            shutil.move(str(f), str(dst / f.name))
            moved.append(f.name)
        rows = self._take_manifest(root_id)
        (dst / "manifest.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        ids = self.bus.tree_ids(root_id)
        entry = {"root_id": root_id, "name": t.name, "status": t.status, "deleted_at": utcnow(),
                 "deleted_epoch": time.time(), "by": by, "folders": moved, "ids": sorted(ids),
                 "artifacts": len(rows), "branches": branches}
        self.trashed[root_id] = entry
        self._save()
        self.bus.hide(root_id, ids)
        self.bus.emit(Event("task_deleted", by, None, root_id, None, f"{t.name} moved to the trash",
                            {"mode": "trash", "name": t.name, "branches": branches,
                             "retention_days": self.retention_days}))
        if self.retention_days > 0:
            entry["purge_at"] = entry["deleted_epoch"] + self.retention_days * 86400
        return entry

    def restore(self, root_id: str, by: str = "user") -> dict:
        e = self.entry(root_id)
        if not e:
            raise KeyError(f"{root_id} is not in the trash")
        src = self._entry_dir(root_id)
        for name in e["folders"]:
            if (self.store.root / name).exists():
                raise ValueError(f"cannot restore {root_id}: {name} already exists")
        for name in e["folders"]:
            shutil.move(str(src / name), str(self.store.root / name))
        mf = src / "manifest.jsonl"
        if mf.is_file():
            with self.store._lock, self.store.manifest.open("a", encoding="utf-8") as f:
                f.write(mf.read_text(encoding="utf-8"))
        shutil.rmtree(src, ignore_errors=True)
        self.trashed.pop(root_id, None)
        self._save()
        self.bus.reload(root_id)
        self.bus.emit(Event("task_restored", by, None, root_id, None, f"{e['name']} restored from the trash",
                            {"name": e["name"], "archived": self.is_archived(root_id)}))
        return {**e, "restored": True}

    # ---- permanent delete -----------------------------------------------
    def purge(self, root_id: str, by: str = "user") -> dict:
        """Remove a trashed task for good."""
        e = self.entry(root_id)
        if not e:
            raise KeyError(f"{root_id} is not in the trash")
        return self._purge(root_id, set(e.get("ids") or []) | {root_id}, e["name"], by, e.get("branches", []))

    def purge_expired(self, now: float | None = None) -> list[str]:
        if self.retention_days <= 0:
            return []
        now = now or time.time()
        done = []
        for e in self.trash_entries():
            if e["purge_at"] <= now:
                self.purge(e["root_id"], by="retention")
                done.append(e["root_id"])
        return done

    def _purge(self, root_id: str, ids: set[str], name: str, by: str, branches: list[dict]) -> dict:
        for f in self.folders(root_id):
            shutil.rmtree(f, ignore_errors=True)
        shutil.rmtree(self._entry_dir(root_id), ignore_errors=True)
        if self.trashed.pop(root_id, None) is not None:
            self._save()
        self._take_manifest(root_id)
        self.bus.on_purged(root_id)
        removed = self.bus.purge_log(root_id, ids)
        self.bus.emit(Event("task_deleted", by, None, root_id, None, f"{name} deleted permanently",
                            {"mode": "permanent", "name": name, "branches": branches, "events_removed": removed}))
        return {"root_id": root_id, "name": name, "permanent": True, "branches": branches,
                "events_removed": removed}

    def _take_manifest(self, root_id: str) -> list[dict]:
        """Remove (and return) the artifact records of a task from the manifest."""
        store = self.store
        with store._lock:
            if not store.manifest.exists():
                return []
            keep, taken = [], []
            for line in store.manifest.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                r = json.loads(line)
                (taken if self.owns(root_id, r.get("root_task_id") or r.get("task_id")) else keep).append(line)
            tmp = store.manifest.with_suffix(".tmp")
            tmp.write_text("".join(l + "\n" for l in keep), encoding="utf-8")
            os.replace(tmp, store.manifest)
            return [json.loads(l) for l in taken]
