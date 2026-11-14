"""Data versions and migrations of the data dir (artifacts/).

artifacts/DATA_VERSION holds the layout version the data was last written with. At start-up (config.build)
pending migrations run in order; each is idempotent. Before the first real migration, the runtime settings
(top-level files, channels/, feeds/, office-assets/; no task data) are saved to
<data>/backups/pre-migrate-v<from>-v<to>-<UTC>.tar.gz. A data dir written by a newer version stops start-up:
run that version again, or restore a backup made before the upgrade.

To add a migration: bump DATA_VERSION and append (version, "what it does", function(artifacts_root)).
"""
from __future__ import annotations

import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

DATA_VERSION = 1
MARKER = "DATA_VERSION"
STATE_DIRS = ("channels", "feeds", "office-assets")


def _baseline(root: Path) -> None:
    """v1: data from before versioning; nothing changes, the marker is written."""


MIGRATIONS: list[tuple[int, str, Callable[[Path], None]]] = [
    (1, "start versioning the data dir", _baseline),
]


class DataTooNew(ValueError):
    pass


def read_data_version(root: Path) -> int:
    """The data dir's version; 0 = written before versioning (or empty)."""
    try:
        return int((Path(root) / MARKER).read_text().strip())
    except (OSError, ValueError):
        return 0


def write_data_version(root: Path, version: int) -> None:
    p = Path(root) / MARKER
    tmp = p.with_suffix(".tmp")
    tmp.write_text(f"{version}\n")
    tmp.replace(p)


def pending(root: Path) -> list[tuple[int, str]]:
    have = read_data_version(root)
    return [(v, what) for v, what, _ in MIGRATIONS if v > have]


def check(root: Path) -> None:
    have = read_data_version(root)
    if have > DATA_VERSION:
        raise DataTooNew(f"the data in {root} is version {have}, newer than this code understands ({DATA_VERSION}): "
                         "start the newer version again, or restore a backup made before the upgrade")


def snapshot(root: Path, frm: int, to: int) -> Path:
    """The runtime settings (no task data) before migrating."""
    out = Path(root).parent / "backups"
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = out / f"pre-migrate-v{frm}-v{to}-{stamp}.tar.gz"
    with tarfile.open(target, "w:gz") as tar:
        for p in sorted(Path(root).iterdir()):
            if (p.is_file() and not p.name.endswith((".tmp", ".tokens.json")) and p.name != "scheduler.lock") \
                    or (p.is_dir() and p.name in STATE_DIRS):
                tar.add(p, arcname=f"artifacts/{p.name}",
                        filter=lambda t: None if t.name.endswith((".tmp", ".tokens.json")) else t)
    return target


def migrate(root: Path) -> list[str]:
    """Run pending migrations; returns what ran. Raises DataTooNew for data from a newer version."""
    root = Path(root)
    check(root)
    have = read_data_version(root)
    todo = [(v, what, fn) for v, what, fn in MIGRATIONS if v > have]
    if not todo:
        return []
    fresh = not any(p for p in root.iterdir() if p.name != MARKER)
    if not fresh and any(v > 1 for v, _, _ in todo):  # v1 changes nothing: no snapshot for it
        snapshot(root, have, todo[-1][0])
    done = []
    for v, what, fn in todo:
        if not fresh:
            fn(root)
        write_data_version(root, v)
        done.append(f"v{v}: {what}")
    return done
