"""Artifact store: timestamped history of every fenced block plus a per-task workspace tree.

Layout under ``artifacts/<root_task>/``:
    workspace/<path>                                  latest version of every named file
    <agent>/<task>/<UTC ts>/<path>                     each named-file version (create/update/patch)
    <agent>/<task>/<UTC ts>/<path>.diff                the patch that produced a version
    <agent>/<task>/<UTC ts>_<n>.<ext>                  unnamed snippets
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import stat
import sys
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .fences import EXT, Fence, normalize_path, parse_fences
from .patching import (PatchError, apply_edit, apply_hunks, is_search_replace, is_unified_diff,
                       parse_search_replace, parse_unified)

SAFE = re.compile(r"[^A-Za-z0-9_.-]+")
MAX_FILE_BYTES = 5_000_000  # text file written by an agent; "limits.workspace_file_mb" (limits.apply_limits)


def fence_kind(f: Fence) -> str:
    """What a fenced block does: truncated | diff | search_replace | file | snippet."""
    if not f.closed:
        return "truncated"
    diffish = f.lang in ("diff", "patch", "udiff")
    if is_unified_diff(f.code) or (diffish and diff_default(f) and re.search(r"^@@", f.code, re.M)):
        return "diff"
    if is_search_replace(f.code):
        return "search_replace"
    return "file" if f.path else "snippet"


def diff_default(f: Fence) -> str | None:
    """The file a diff fence's bare hunks apply to: its path, unless the path names a .diff/.patch file."""
    return None if (f.path or "").endswith((".diff", ".patch")) else f.path


def diff_targets(diff: str, default_path: str | None = None) -> list[str]:
    """Workspace files a unified diff (or bare hunks for default_path) changes."""
    if is_unified_diff(diff):
        return list(dict.fromkeys(p for fp in parse_unified(diff) for p in (fp.new_path, fp.old_path) if p))
    return [default_path] if default_path else []


def fence_targets(f: Fence) -> list[str]:
    """Workspace files a fenced block would write or change; [] when it is only kept as a snippet."""
    kind = fence_kind(f)
    if kind == "diff":
        return diff_targets(f.code, diff_default(f))
    if kind == "search_replace":
        try:
            return list(dict.fromkeys(t for p, _, _ in parse_search_replace(f.code)
                                      if (t := normalize_path(p) or f.path)))
        except PatchError:
            return []
    return [f.path] if kind == "file" else []


def extract_fenced(text: str) -> list[tuple[str, str]]:
    """Return [(language, code)] for every fenced block in text."""
    return [(f.lang, f.code) for f in parse_fences(text)]


def _in_git(p: Path, ws: Path) -> bool:
    return p.relative_to(ws).parts[0] == ".git"


# "source ⬇": dependency, build and cache folders left out of a source-only zip.
# Skipped wherever they appear:
SOURCE_SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
               ".tox", ".nox", ".eggs", ".next", ".nuxt", ".output", ".svelte-kit", ".turbo", ".parcel-cache", ".cache",
               ".gradle", ".dart_tool", "dist", "build", "coverage", "htmlcov"}
# Skipped only next to the project file that owns them (a plain bin/ or vendor/ can be real source):
SOURCE_SKIP_BESIDE = {"target": ("Cargo.toml", "pom.xml"), "vendor": ("go.mod", "composer.json", "Gemfile"),
                      "bin": (".csproj", ".fsproj", ".vbproj", ".sln", ".slnx"),
                      "obj": (".csproj", ".fsproj", ".vbproj", ".sln", ".slnx")}


def source_files(ws: Path) -> list[Path]:
    """Workspace files without dependency, build and cache folders (Node, Python, Go, Rust, .NET, Java, PHP, Ruby).
    Pruned while walking, so a large node_modules is never read; any folder with a pyvenv.cfg is a venv."""
    out: list[Path] = []
    for d, dirs, files in os.walk(ws):
        names = set(files) | set(dirs)
        lower = [n.lower() for n in names]

        def owned(owners: tuple) -> bool:
            return any(n == o.lower() or (o.startswith(".") and n.endswith(o)) for o in owners for n in lower)
        dirs[:] = sorted(x for x in dirs if x not in SOURCE_SKIP
                         and not (x in SOURCE_SKIP_BESIDE and owned(SOURCE_SKIP_BESIDE[x]))
                         and not (Path(d) / x / "pyvenv.cfg").is_file())
        out += [Path(d) / f for f in sorted(files) if f not in (".DS_Store", "Thumbs.db") and not f.endswith((".pyc", ".pyo"))]
    return [p for p in out if p.is_file()]


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


class ArtifactStore:
    def __init__(self, root: str | Path = "artifacts"):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest = self.root / "manifest.jsonl"
        self._lock = threading.RLock()
        self.git = None  # GitRepo: commit every workspace version (set by config.build)

    # ---- paths ----------------------------------------------------------
    def workspace(self, root_id: str) -> Path:
        return self.root / SAFE.sub("_", root_id) / "workspace"

    def env_dir(self, root_id: str) -> Path:
        """The task's virtual environment for run_code (outside the workspace: never zipped or committed)."""
        return self.root / SAFE.sub("_", root_id) / ".venv"

    def _hist(self, root_id: str, agent: str, task_id: str) -> Path:
        return self.root / SAFE.sub("_", root_id) / SAFE.sub("_", agent) / SAFE.sub("_", task_id)

    def _ws_file(self, root_id: str, rel: str) -> tuple[str, Path]:
        norm = normalize_path(rel)
        if not norm:
            raise PatchError(f"invalid file path {rel!r}")
        if norm.split("/", 1)[0].lower() == ".git":
            raise PatchError(f"{rel!r}: the .git folder is managed by the xqo")
        return norm, self.workspace(root_id) / norm

    def _commit(self, root_id: str, rec: dict) -> dict:
        """Commit one workspace change; git problems are recorded, never raised."""
        if not (self.git and self.git.enabled) or rec["action"] == "unchanged":
            return rec
        try:
            sha = self.git.commit(self.workspace(root_id), [rec["file"]],
                                  f"{rec['action']} {rec['file']} by {rec['agent']} ({rec['task_id']})", rec["agent"])
            if sha:
                rec["commit"] = sha
        except Exception as e:  # GitError, OSError, TimeoutExpired
            rec["git_error"] = str(e)[:500]
        return rec

    def _record(self, rec: dict) -> dict:
        with self.manifest.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        return rec

    def _rel(self, p: Path) -> str:
        return str(p.relative_to(self.root)).replace("\\", "/")

    def write_meta(self, root_id: str, meta: dict) -> None:
        """Write task.json (id, title, prompt) at the top of a task's artifact folder."""
        folder = self.root / SAFE.sub("_", root_id)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "task.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    def task_meta(self, root_id: str) -> dict:
        p = self.root / SAFE.sub("_", root_id) / "task.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}

    # ---- workspace operations ------------------------------------------
    def read_file(self, root_id: str, rel: str) -> str:
        norm, p = self._ws_file(root_id, rel)
        if not p.is_file():
            raise PatchError(f"file {norm!r} does not exist in this task's workspace")
        try:
            return p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise PatchError(f"{norm!r} is a binary file ({p.stat().st_size} bytes): it can't be read as text; "
                             "it can still be sent on, e.g. with send_to_peer") from None

    def read_bytes(self, root_id: str, rel: str) -> tuple[str, bytes]:
        norm, p = self._ws_file(root_id, rel)
        if not p.is_file():
            raise PatchError(f"file {norm!r} does not exist in this task's workspace")
        return norm, p.read_bytes()

    def write_bytes(self, root_id: str, rel: str, data: bytes, task_id: str, agent: str,
                    action: str | None = None, source: str | None = None, commit: bool = True) -> dict:
        """Store a file as is (e.g. a binary file received from a peer): workspace, history and git."""
        with self._lock:
            norm, wp = self._ws_file(root_id, rel)
            existed = wp.is_file()
            action = action or ("update" if existed else "create")
            wp.parent.mkdir(parents=True, exist_ok=True)
            wp.write_bytes(data)
            ts = _ts()
            hp = self._hist(root_id, agent, task_id) / ts / norm
            hp.parent.mkdir(parents=True, exist_ok=True)
            hp.write_bytes(data)
            rec = {"root_task_id": root_id, "task_id": task_id, "agent": agent, "timestamp": ts,
                   "language": "binary", "bytes": len(data), "path": self._rel(hp), "file": norm, "action": action,
                   "workspace": self._rel(wp), "source": source}
            return self._record(self._commit(root_id, rec) if commit else rec)

    def list_files(self, root_id: str) -> list[dict]:
        ws = self.workspace(root_id)
        if not ws.exists():
            return []
        return [{"file": str(p.relative_to(ws)).replace("\\", "/"), "bytes": p.stat().st_size,
                 "updated": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat()}
                for p in sorted(ws.rglob("*")) if p.is_file() and not _in_git(p, ws)]

    def write_file(self, root_id: str, rel: str, content: str, task_id: str, agent: str,
                   lang: str = "", action: str | None = None, diff: str | None = None,
                   source: str | None = None, commit: bool = True) -> dict:
        if len(content.encode()) > MAX_FILE_BYTES:
            raise PatchError(f"{rel}: file exceeds {MAX_FILE_BYTES} bytes (limits.workspace_file_mb)")
        with self._lock:
            norm, wp = self._ws_file(root_id, rel)
            existed = wp.is_file()
            if existed and action is None and wp.read_text(encoding="utf-8") == content:
                action = "unchanged"
            action = action or ("update" if existed else "create")
            wp.parent.mkdir(parents=True, exist_ok=True)
            wp.write_text(content, encoding="utf-8")
            ts = _ts()
            hp = self._hist(root_id, agent, task_id) / ts / norm
            hp.parent.mkdir(parents=True, exist_ok=True)
            hp.write_text(content, encoding="utf-8")
            rec = {"root_task_id": root_id, "task_id": task_id, "agent": agent, "timestamp": ts,
                   "language": lang or "text", "bytes": len(content.encode()), "path": self._rel(hp),
                   "file": norm, "action": action, "workspace": self._rel(wp), "source": source}
            if diff is not None:
                dp = hp.with_name(hp.name + ".diff")
                dp.write_text(diff, encoding="utf-8")
                rec["patch_path"] = self._rel(dp)
            return self._record(self._commit(root_id, rec) if commit else rec)

    def rollback(self, root_id: str, rel: str, version_path: str, agent: str = "user") -> dict:
        """Restore a file to an earlier version (recorded as a new 'rollback' version)."""
        norm = normalize_path(rel)
        rec = next((r for r in self.history(root_id, norm) if r.get("path") == version_path), None)
        src = self.resolve(version_path) if rec else None
        if not src:
            raise PatchError(f"version {version_path!r} of {rel!r} not found")
        if rec.get("language") == "binary":
            return self.write_bytes(root_id, norm, src.read_bytes(), rec["task_id"], agent, action="rollback",
                                    source=f"rollback:{rec['timestamp']}")
        return self.write_file(root_id, norm, src.read_text(encoding="utf-8"), rec["task_id"], agent,
                               rec.get("language", ""), action="rollback", source=f"rollback:{rec['timestamp']}")

    def state_from(self, records: list[dict]) -> dict[str, str | bytes]:
        """Workspace contents after applying version records in order (e.g. a task's artifact events).
        Binary files (language "binary") come back as bytes."""
        files: dict[str, str | bytes] = {}
        for r in records:
            if not r.get("file") or r.get("action") in ("unchanged", "patch_failed", "truncated"):
                continue
            if r["action"] == "delete":
                files.pop(r["file"], None)
                if r.get("folder"):  # delete_path on a folder: everything under it goes
                    for f in [f for f in files if f.startswith(r["file"] + "/")]:
                        del files[f]
            elif r.get("path") and (p := self.resolve(r["path"])):
                files[r["file"]] = p.read_bytes() if r.get("language") == "binary" else p.read_text(encoding="utf-8")
        return files

    def restore(self, root_id: str, files: dict[str, str | bytes], agent: str, source: str,
                message: str) -> list[dict]:
        """Write a set of files as 'fork' versions in a single commit."""
        with self._lock:
            recs = [self.write_bytes(root_id, f, c, root_id, agent, action="fork", source=source, commit=False)
                    if isinstance(c, bytes) else
                    self.write_file(root_id, f, c, root_id, agent, action="fork", source=source, commit=False)
                    for f, c in sorted(files.items())]
            ws = self.workspace(root_id)
            if self.git and self.git.enabled:
                try:
                    self.git.init(ws)
                    sha = self.git.commit(ws, ["."], message, agent)
                    for r in recs:
                        r["commit"] = sha
                except Exception as e:
                    for r in recs:
                        r["git_error"] = str(e)[:500]
            return recs

    def final_commit(self, root_id: str, message: str, author: str) -> str | None:
        """Commit whatever is in the workspace now, plus a marker (used when a task is cancelled)."""
        ws = self.workspace(root_id)
        if not (self.git and self.git.enabled and ws.is_dir()):
            return None
        with self._lock:
            try:
                return self.git.commit(ws, ["."], message, author, allow_empty=True)
            except Exception:  # git problems never block a cancel
                return None

    def git_log(self, root_id: str) -> list[dict]:
        return self.git.log(self.workspace(root_id)) if self.git else []

    def delete_file(self, root_id: str, rel: str, task_id: str, agent: str) -> dict:
        with self._lock:
            norm, wp = self._ws_file(root_id, rel)
            if not wp.is_file():
                raise PatchError(f"cannot delete {norm!r}: not in workspace")
            wp.unlink()
            return self._record(self._commit(root_id, {
                "root_task_id": root_id, "task_id": task_id, "agent": agent, "timestamp": _ts(),
                "language": "text", "bytes": 0, "path": None, "file": norm, "action": "delete", "workspace": None}))

    def delete_path(self, root_id: str, rel: str, task_id: str, agent: str) -> dict:
        """Delete a workspace file or a whole folder (e.g. a leftover node_modules): one record, one commit."""
        with self._lock:
            norm, wp = self._ws_file(root_id, rel)
            if wp.is_file() or wp.is_symlink():
                return self.delete_file(root_id, norm, task_id, agent) if wp.is_file() else self._unlink(root_id, norm, wp, task_id, agent)
            if not wp.is_dir():
                raise PatchError(f"cannot delete {norm!r}: not in workspace")
            count = sum(1 for p in wp.rglob("*") if p.is_file())
            shutil.rmtree(wp, **{_RMTREE_HOOK: _force_remove})
            if wp.exists():
                raise PatchError(f"could not delete all of {norm!r} (files in use or not writable)")
            rec = {"root_task_id": root_id, "task_id": task_id, "agent": agent, "timestamp": _ts(),
                   "language": "text", "bytes": 0, "path": None, "file": norm, "action": "delete", "workspace": None,
                   "folder": True, "files": count}
            if self.git and self.git.enabled:
                try:
                    sha = self.git.commit(self.workspace(root_id), [norm],
                                          f"delete {norm}/ ({count} files) by {agent} ({task_id})", agent)
                    if sha:
                        rec["commit"] = sha
                except Exception as e:  # an untracked or ignored folder has nothing to commit
                    if "did not match" not in str(e) and "ignored" not in str(e):
                        rec["git_error"] = str(e)[:500]
            return self._record(rec)

    def _unlink(self, root_id: str, norm: str, wp: Path, task_id: str, agent: str) -> dict:
        wp.unlink()  # a symlink (e.g. node_modules/.bin entries): remove the link, never its target
        return self._record(self._commit(root_id, {
            "root_task_id": root_id, "task_id": task_id, "agent": agent, "timestamp": _ts(),
            "language": "text", "bytes": 0, "path": None, "file": norm, "action": "delete", "workspace": None}))

    def move_file(self, root_id: str, src: str, dst: str, task_id: str, agent: str) -> list[dict]:
        """Move or rename a workspace file (the agents' `git mv`): one commit, so git sees a rename."""
        with self._lock:
            norm_src, sp = self._ws_file(root_id, src)
            norm_dst, dp = self._ws_file(root_id, dst)
            if not sp.is_file():
                raise PatchError(f"cannot move {norm_src!r}: not in workspace")
            if norm_dst == norm_src:
                raise PatchError(f"{norm_src!r} is already at that path")
            if dp.exists():
                raise PatchError(f"cannot move to {norm_dst!r}: it already exists (delete or patch it instead)")
            content = sp.read_text(encoding="utf-8")
            rec = self.write_file(root_id, norm_dst, content, task_id, agent, action="move",
                                  source=f"move:{norm_src}", commit=False)
            sp.unlink()
            gone = {"root_task_id": root_id, "task_id": task_id, "agent": agent, "timestamp": rec["timestamp"],
                    "language": "text", "bytes": 0, "path": None, "file": norm_src, "action": "delete",
                    "workspace": None, "source": f"move:{norm_dst}"}
            if self.git and self.git.enabled:
                try:
                    sha = self.git.commit(self.workspace(root_id), [norm_src, norm_dst],
                                          f"move {norm_src} -> {norm_dst} by {agent} ({task_id})", agent)
                    if sha:
                        rec["commit"] = gone["commit"] = sha
                except Exception as e:  # GitError, OSError, TimeoutExpired
                    rec["git_error"] = gone["git_error"] = str(e)[:500]
            return [rec, self._record(gone)]

    def patch_file(self, root_id: str, rel: str, task_id: str, agent: str,
                   edits: list[dict] | None = None, diff: str | None = None) -> list[dict]:
        """Patch via exact edits [{old, new}] or a unified diff (applied to `rel` if it has no headers)."""
        if not edits and not diff:
            raise PatchError("provide 'edits' or 'diff'")
        with self._lock:
            if diff:
                return self.apply_diff(root_id, diff, task_id, agent, default_path=rel)
            cur = self.read_file(root_id, rel)
            new = cur
            for k, e in enumerate(edits, 1):
                try:
                    new = apply_edit(new, e.get("old", ""), e.get("new", ""))
                except PatchError as err:
                    raise PatchError(f"{rel} edit {k}: {err}") from None
            summary = "\n".join(f"<<<<<<< SEARCH\n{e.get('old', '')}\n=======\n{e.get('new', '')}\n>>>>>>> REPLACE"
                                for e in edits)
            return [self.write_file(root_id, rel, new, task_id, agent, action="patch", diff=summary,
                                    source="tool")]

    def apply_diff(self, root_id: str, diff: str, task_id: str, agent: str,
                   default_path: str | None = None) -> list[dict]:
        with self._lock:
            if is_unified_diff(diff):
                files = parse_unified(diff)
            elif default_path and re.search(r"^@@", diff, re.M):  # bare hunks + path from the fence
                files = parse_unified(f"--- a/{default_path}\n+++ b/{default_path}\n{diff}")
            else:
                raise PatchError("not a unified diff (needs ---/+++ headers and @@ hunks)")
            # validate every file first so a multi-file patch applies all-or-nothing
            plan = []
            for fp in files:
                if fp.new_path is None:
                    plan.append(("delete", fp.old_path, None))
                    continue
                if fp.old_path is None:
                    base = ""
                else:
                    try:
                        base = self.read_file(root_id, fp.old_path)
                    except PatchError:
                        if any(l[:1] in (" ", "-") and l[1:].strip() for h in fp.hunks for l in h[1:]):
                            raise
                        base = ""  # file never seen, but the diff only adds lines
                try:
                    plan.append(("patch", fp.new_path, apply_hunks(base, fp.hunks)))
                except PatchError as e:
                    raise PatchError(f"{fp.new_path}: {e}") from None
                if (fp.old_path and normalize_path(fp.old_path) != normalize_path(fp.new_path)
                        and self._ws_file(root_id, fp.old_path)[1].is_file()):
                    plan.append(("delete", fp.old_path, None))  # rename diff: drop the old path
            recs = []
            for action, path, content in plan:
                if action == "delete":
                    recs.append(self.delete_file(root_id, path, task_id, agent))
                else:
                    recs.append(self.write_file(root_id, path, content, task_id, agent,
                                                action="patch", diff=diff, source="diff"))
            return recs

    def apply_search_replace(self, root_id: str, text: str, task_id: str, agent: str,
                             default_path: str | None) -> list[dict]:
        with self._lock:
            pending: dict[str, str] = {}
            for path, old, new in parse_search_replace(text):
                target = normalize_path(path) or default_path
                if not target:
                    raise PatchError("SEARCH/REPLACE block has no file name")
                if target not in pending:
                    try:
                        pending[target] = self.read_file(root_id, target)
                    except PatchError:
                        if old.strip():
                            raise
                        pending[target] = ""
                pending[target] = apply_edit(pending[target], old, new)
            return [self.write_file(root_id, p, c, task_id, agent, action="patch", diff=text,
                                    source="search_replace") for p, c in pending.items()]

    # ---- snippets & extraction -----------------------------------------
    def save(self, code: str, lang: str, task_id: str, agent: str, root_id: str | None = None,
             action: str = "snippet", error: str | None = None) -> dict:
        root_id = root_id or task_id
        with self._lock:
            folder = self._hist(root_id, agent, task_id)
            folder.mkdir(parents=True, exist_ok=True)
            n = sum(1 for p in folder.iterdir() if p.is_file()) + 1
            ts = _ts()
            ext = EXT.get(lang, SAFE.sub("", lang) or "txt")
            path = folder / f"{ts}_{n}.{ext}"
            path.write_text(code, encoding="utf-8")
            rec = {"root_task_id": root_id, "task_id": task_id, "agent": agent, "timestamp": ts,
                   "language": lang or "text", "bytes": len(code.encode()), "path": self._rel(path),
                   "file": None, "action": action}
            if error:
                rec["error"] = error
            return self._record(rec)

    def process_text(self, text: str, task_id: str, agent: str, root_id: str | None = None,
                     allow: Callable[[str], bool] | None = None) -> tuple[list[dict], list[str]]:
        """Extract every fenced block: write named files, apply patches, keep snippets.

        allow: checks each workspace file a block would change; a block touching a refused file is kept
        as a snippet with action 'blocked' (its 'blocked' field lists those files) and changes nothing.
        Returns (records, errors); errors are patch failures to report back to the model."""
        root_id = root_id or task_id
        recs, errors = [], []
        for f in parse_fences(text):
            denied = [p for p in fence_targets(f) if not allow(p)] if allow else []
            if denied:
                rec = self.save(f.code, f.lang or "text", task_id, agent, root_id, action="blocked",
                                error=f"not allowed to write {', '.join(denied)}")
                recs.append({**rec, "blocked": denied})
                continue
            try:
                recs.extend(self._handle(f, task_id, agent, root_id))
            except PatchError as e:
                errors.append(f"{f.path or f.lang or 'block'} (fence at line {f.start + 1}): {e}")
                recs.append(self.save(f.code, f.lang or "diff", task_id, agent, root_id,
                                      action="patch_failed", error=str(e)))
        return recs, errors

    def _handle(self, f: Fence, task_id: str, agent: str, root_id: str) -> list[dict]:
        kind = fence_kind(f)
        if kind == "truncated":  # truncated reply: keep as snippet, never overwrite workspace files
            return [self.save(f.code, f.lang, task_id, agent, root_id, action="truncated")]
        if kind == "diff":
            return self.apply_diff(root_id, f.code, task_id, agent, default_path=diff_default(f))
        if kind == "search_replace":
            return self.apply_search_replace(root_id, f.code, task_id, agent, f.path)
        if kind == "file":
            return [self.write_file(root_id, f.path, f.code, task_id, agent, f.lang, source=f.source)]
        return [self.save(f.code, f.lang, task_id, agent, root_id)]

    def save_from_text(self, text: str, task_id: str, agent: str, root_id: str | None = None) -> list[dict]:
        return self.process_text(text, task_id, agent, root_id)[0]

    # ---- reports (Markdown reports agents write; listed per agent in the office) ----
    def save_report(self, root_id: str, task_id: str, agent: str, title: str, markdown: str,
                    status: str = "done", auto: bool = False) -> dict:
        """Save a Markdown report as <root>/<agent>/reports/<agent>_<timestamp>_<title>.md (artifact history)."""
        with self._lock:
            folder = self.root / SAFE.sub("_", root_id) / SAFE.sub("_", agent) / "reports"
            folder.mkdir(parents=True, exist_ok=True)
            ts = _ts()
            slug = SAFE.sub("-", (title or "report").lower()).strip("-.")[:48] or "report"
            path = folder / f"{SAFE.sub('_', agent)}_{ts}_{slug}.md"
            n = 2
            while path.exists():
                path = folder / f"{SAFE.sub('_', agent)}_{ts}_{slug}-{n}.md"
                n += 1
            path.write_text(markdown, encoding="utf-8")
            return self._record({"root_task_id": root_id, "task_id": task_id, "agent": agent, "timestamp": ts,
                                 "language": "markdown", "bytes": len(markdown.encode()), "path": self._rel(path),
                                 "file": None, "action": "report", "title": title or "Report", "status": status,
                                 "auto": auto})

    def reports(self, agent: str | None = None, limit: int = 10) -> list[dict]:
        """Newest first."""
        out = []
        for r in reversed(self.list()):
            if r.get("action") == "report" and (agent is None or r["agent"] == agent) and self.resolve(r["path"]):
                out.append(r)  # a deleted task's reports are gone with its folder
                if len(out) >= limit:
                    break
        return out

    # ---- listing / download --------------------------------------------
    def list(self, task_id: str | None = None) -> list[dict]:
        if not self.manifest.exists():
            return []
        rows = [json.loads(l) for l in self.manifest.read_text(encoding="utf-8").splitlines() if l.strip()]
        return [r for r in rows if task_id is None or task_id in (r["task_id"], r.get("root_task_id"))]

    def history(self, root_id: str, rel: str) -> list[dict]:
        norm = normalize_path(rel)
        return [r for r in self.list(root_id) if r.get("file") == norm]

    def resolve(self, rel: str) -> Path | None:
        p = (self.root / rel).resolve()
        if self.root not in p.parents or ".git" in p.relative_to(self.root).parts:
            return None
        return p if p.is_file() else None

    def zip_task(self, task_id: str, workspace_only: bool = False, source_only: bool = False) -> bytes:
        """source_only: the workspace's project files only (no history, departments, dependency or build folders)."""
        if source_only:
            workspace_only = True
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            roots = {r.get("root_task_id") or r["task_id"] for r in self.list(task_id)} or {task_id}
            for root_id in roots:
                ws = self.workspace(root_id)
                if ws.exists():
                    for p in source_files(ws) if source_only else sorted(ws.rglob("*")):
                        if p.is_file() and not _in_git(p, ws):
                            z.write(p, ("" if workspace_only else "workspace/") + str(p.relative_to(ws)))
            # department workspaces of this task tree (artifacts/<task>.<department>/workspace)
            subs = [] if workspace_only else sorted({r["root_task_id"] for r in self.list() if any(
                (r.get("root_task_id") or "").startswith(root + ".") for root in roots)})
            for sub in subs:
                ws = self.workspace(sub)
                for p in sorted(ws.rglob("*")) if ws.exists() else []:
                    if p.is_file() and not _in_git(p, ws):
                        z.write(p, f"departments/{sub.rsplit('.', 1)[1]}/{p.relative_to(ws)}")
            if not workspace_only:
                for r in self.list(task_id) + [r for s in subs for r in self.list(s)]:
                    for key in ("path", "patch_path"):
                        p = self.resolve(r[key]) if r.get(key) else None
                        if p:
                            z.write(p, "history/" + r[key])
        return buf.getvalue()


_RMTREE_HOOK = "onexc" if sys.version_info >= (3, 12) else "onerror"  # onerror is deprecated in 3.12


def _force_remove(func, path, _exc) -> None:
    """shutil.rmtree onerror: clear the read-only bit (Windows, npm/git files) and retry once."""
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        func(path)
    except OSError:
        pass
