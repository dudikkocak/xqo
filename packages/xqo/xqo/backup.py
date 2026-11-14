"""Backups of one instance: its configuration, its runtime settings and (optionally) all task data.

    xqo backup [--scope full|state|config] [--keep N]      through the running server, else offline
    xqo restore ARCHIVE --to DIR [--dry-run]                into a new, empty folder

An archive (xqo-backup-<UTC>-<scope>.tar.gz) holds:
    MANIFEST.json      format, scope, code and data versions, running tasks, sha256 + size per file
    config/...         agents.json, departments/, agency files, skills/, avatars it references
    data/artifacts/... runtime settings (schedules, office, usage, archive, channel, feed and peering state) and, for
                       scope "full", every task's workspace, history, attachments and notes
    data/logs/...      the event log (scope "full")

Never included: channel sign-in tokens (artifacts/channels/*.tokens.json: sign in again after a restore),
secrets (they live in environment variables), virtual environments, run copies, *.tmp, scheduler.lock and the
backups folder itself. Through a running server the scheduler, feeds and channel polling pause while the
archive is written (seconds); tasks that are running keep running and are listed in the manifest.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import time
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

from . import __version__
from .departments import PACKAGED_DEPARTMENTS
from .migrations import DATA_VERSION, read_data_version

FORMAT = 1
SCOPES = ("full", "state", "config")
PREFIX = "xqo-backup-"
STATE_DIRS = {"channels", "feeds", "office-assets", ".peers"}  # runtime settings kept in folders under artifacts/
SKIP_DIRS = {".venv", ".venv-docker", "__pycache__", "backups"}
SKIP_FILES = ("*.tmp", "scheduler.lock", "*.tokens.json")
AGENCY_FILES = ("agency.json", "agency.lock.json", "AGENCY_AGENTS_LICENSE")


def _skip(rel: PurePosixPath) -> bool:
    return any(p in SKIP_DIRS for p in rel.parts[:-1]) or any(fnmatch(rel.name, g) for g in SKIP_FILES)


def _walk(folder: Path, arc: str, out: dict[str, Path], skip_under: set[Path] = frozenset()) -> None:
    if not folder.is_dir():
        return
    for p in sorted(folder.rglob("*")):
        if not p.is_file() or p.is_symlink() or any(s == p or s in p.parents for s in skip_under):
            continue
        rel = PurePosixPath(p.relative_to(folder).as_posix())
        if rel.parts and rel.parts[0] in SKIP_DIRS or _skip(rel):
            continue
        out[f"{arc}/{rel}"] = p


def _config_arc(path: Path, cfg_dir: Path) -> str:
    try:
        return f"config/{path.resolve().relative_to(cfg_dir.resolve()).as_posix()}"
    except ValueError:  # outside the config folder: kept under its own name
        return f"config/_external/{path.name}"


def config_files(bus) -> dict[str, Path]:
    """The instance's configuration: {archive name: file}."""
    ctx = bus.department_ctx
    cfg_path = Path(ctx["config_path"])
    out: dict[str, Path] = {}
    if not cfg_path.is_file():
        return out
    cfg_dir = cfg_path.parent
    out[_config_arc(cfg_path, cfg_dir)] = cfg_path
    for name in AGENCY_FILES:
        if (cfg_dir / name).is_file():
            out[f"config/{name}"] = cfg_dir / name
    folder = ctx.get("folder")
    if folder and Path(folder).resolve() != PACKAGED_DEPARTMENTS.resolve():
        for p in sorted(Path(folder).glob("*.json")):
            out[_config_arc(p, cfg_dir)] = p
    library = getattr(bus, "skills", None)
    for f in (library.folders if library else []):
        _walk(Path(f), _config_arc(Path(f), cfg_dir), out)
    for avatar in _avatars(bus):
        out[_config_arc(avatar, cfg_dir)] = avatar
    return out


def _avatars(bus) -> list[Path]:
    found = []
    for info in [getattr(bus, "user_avatar", None)] + [getattr(a, "avatar", None) for a in bus.agents.values()]:
        p = (info or {}).get("path") if isinstance(info, dict) else None
        if p and Path(p).is_file():
            found.append(Path(p))
    return found


def data_files(bus, scope: str, data_dir: Path) -> dict[str, Path]:
    root = Path(bus.artifacts.root)
    out: dict[str, Path] = {}
    run_dir = (bus.sandbox.config.run_dir or "") if getattr(bus, "sandbox", None) else ""
    skip = {Path(run_dir).expanduser().resolve()} if run_dir else set()
    skip.add((data_dir / "backups").resolve())
    if scope == "full":
        _walk(root, "data/artifacts", out, skip)
        store = getattr(bus, "store", None)  # store.py: the jsonl file
        if store is not None:
            import tempfile
            scratch = Path(tempfile.gettempdir()) / f"xqo-backup-{os.getpid()}"
            for name, path in store.backup_files(scratch).items():
                out["data/logs/" + name] = path
        else:
            log = Path(bus.log_path)
            if log.is_file():
                out["data/logs/" + log.name] = log
        return out
    for p in sorted(root.iterdir()) if root.is_dir() else []:
        if p.is_file() and not _skip(PurePosixPath(p.name)):
            out[f"data/artifacts/{p.name}"] = p
        elif p.is_dir() and p.name in STATE_DIRS:
            _walk(p, f"data/artifacts/{p.name}", out, skip)
    return out


def _sha256(p: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def running_tasks(bus) -> list[dict]:
    return [{"id": t.id, "name": t.name, "status": t.status} for t in bus.tasks.values()
            if t.parent_id is None and bus.is_running(t.id)]


def create(bus, scope: str = "full", out_dir: str | Path | None = None, data_dir: str | Path = ".",
           keep: int | None = None) -> dict:
    """Write one archive; returns its summary. Runs in a worker thread when called by the server."""
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {', '.join(SCOPES)}")
    data_dir = Path(data_dir).resolve()
    out = Path(out_dir) if out_dir else data_dir / "backups"
    out.mkdir(parents=True, exist_ok=True)
    files = config_files(bus)
    if scope != "config":
        files.update(data_files(bus, scope, data_dir))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = out / f"{PREFIX}{stamp}-{scope}.tar.gz"
    tmp = target.with_suffix(".gz.tmp")
    entries, skipped = [], []
    with tarfile.open(tmp, "w:gz") as tar:
        for arc, p in files.items():
            try:
                digest, size = _sha256(p)
                tar.add(p, arcname=arc, recursive=False)
            except OSError as e:  # a file removed while the archive was written
                skipped.append(f"{arc}: {e}")
                continue
            entries.append({"path": arc, "sha256": digest, "size": size})
        manifest = {"format": FORMAT, "scope": scope, "created": stamp, "version": __version__,
                    "data_version": read_data_version(Path(bus.artifacts.root)), "code_data_version": DATA_VERSION,
                    "config": _config_arc(Path(cfg), Path(cfg).parent) if (cfg := bus.department_ctx["config_path"])
                    and Path(cfg).is_file() else None,
                    "running_tasks": running_tasks(bus), "skipped": skipped,
                    "excluded": ["channel sign-in tokens", "environment variables / secrets", "virtual environments",
                                 "run copies", "backups"], "files": entries}
        data = json.dumps(manifest, indent=1).encode()
        info = tarfile.TarInfo("MANIFEST.json")
        info.size, info.mtime = len(data), int(time.time())
        tar.addfile(info, io.BytesIO(data))
    os.replace(tmp, target)
    removed = prune(out, keep) if keep else []
    return {"path": str(target), "scope": scope, "files": len(entries), "bytes": target.stat().st_size,
            "running_tasks": manifest["running_tasks"], "skipped": skipped, "pruned": removed}


def archives(folder: Path, scope: str = "*") -> list[Path]:
    """Archives in `folder`, oldest first."""
    return sorted(folder.glob(f"{PREFIX}*-{scope}.tar.gz"))


def prune(folder: Path, keep: int) -> list[str]:
    """Keep the newest `keep` archives of each scope."""
    removed = []
    for scope in SCOPES:
        old = archives(folder, scope)[:-keep] if keep > 0 else []
        for p in old:
            p.unlink(missing_ok=True)
            removed.append(p.name)
    return removed


def read_manifest(archive: str | Path) -> dict:
    with tarfile.open(archive, "r:gz") as tar:
        f = tar.extractfile("MANIFEST.json")
        if f is None:
            raise ValueError("not a backup archive: MANIFEST.json is missing")
        return json.loads(f.read())


def verify(archive: str | Path) -> dict:
    """Check every file against the manifest; returns the manifest. Raises ValueError on any mismatch."""
    m = read_manifest(archive)
    if m.get("format") != FORMAT:
        raise ValueError(f"unknown backup format {m.get('format')!r}")
    want = {e["path"]: e for e in m["files"]}
    seen = set()
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            if member.name == "MANIFEST.json":
                continue
            _safe_name(member)
            e = want.get(member.name)
            if e is None:
                raise ValueError(f"{member.name} is in the archive but not in the manifest")
            f = tar.extractfile(member)
            if f is None or hashlib.sha256(f.read()).hexdigest() != e["sha256"]:
                raise ValueError(f"{member.name}: checksum does not match")
            seen.add(member.name)
    missing = set(want) - seen
    if missing:
        raise ValueError(f"{len(missing)} file(s) listed in the manifest are missing, e.g. {sorted(missing)[0]}")
    return m


def _safe_name(member: tarfile.TarInfo) -> None:
    p = PurePosixPath(member.name)
    if not member.isfile() or p.is_absolute() or ".." in p.parts or p.parts[0] not in ("config", "data"):
        raise ValueError(f"unsafe entry in the archive: {member.name}")


def restore(archive: str | Path, to: str | Path, dry_run: bool = False) -> dict:
    """Unpack into a new folder: config/ becomes the folder itself (agents.json, departments/, ...), data/ its
    data dir (artifacts/, logs/). Never writes into a folder that has files."""
    m = verify(archive)
    dest = Path(to)
    if dest.exists() and any(dest.iterdir()):
        raise ValueError(f"{dest} is not empty: restore into a new folder, then point the instance at it")
    summary = {"to": str(dest), "scope": m["scope"], "files": len(m["files"]), "version": m.get("version"),
               "data_version": m.get("data_version"), "running_tasks": m.get("running_tasks") or [],
               "external": [e["path"] for e in m["files"] if e["path"].startswith("config/_external/")]}
    if dry_run:
        return summary
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            if member.name == "MANIFEST.json":
                continue
            _safe_name(member)
            top, rest = member.name.split("/", 1)
            target = dest / rest  # config/x -> x ; data/artifacts/y -> artifacts/y
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(member)
            with target.open("wb") as f:
                f.write(src.read())
    return summary
