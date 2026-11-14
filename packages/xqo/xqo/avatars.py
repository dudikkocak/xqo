"""Optional avatar per agent (and for the user node) on the dashboard graph and Agents panel.

agents.json:
    "user_avatar": "avatars/me.png",                       # optional, the "user" node
    "agents": [{"name": "coder", "avatar": "avatars/coder.png", ...},   # image file next to the config
               {"name": "reviewer", "avatar": "https://example.com/r.webp"},  # image URL (loaded by the browser)
               {"name": "tester", "avatar": "🧪"}]                       # emoji or up to 4 letters

Drop-in folder: an agent without "avatar" gets `<avatars_dir>/<agent name>.<png|jpg|jpeg|webp|gif|svg>` when that
file exists, and the user node gets `<avatars_dir>/user.<ext>`. "avatars_dir" (top level of agents.json, default
"avatars", relative to agents.json, false = off) is the only folder looked in; an explicit "avatar" wins. It is read at
startup and by the dashboard's reload button, so a new image is picked up without a restart.

Files are resolved relative to the file that names them (agents.json, or a department file), must be
png / jpg / gif / webp / svg up to 2 MB, and are served by the dashboard at /api/avatars/<name>. A missing or
unsupported file stops startup with a clear error.
"""
from __future__ import annotations

import re
from pathlib import Path

TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
         ".webp": "image/webp", ".svg": "image/svg+xml"}
MAX_BYTES = 2 * 1024 * 1024
MAX_TEXT = 4
URL = re.compile(r"^https?://\S+$", re.I)


def resolve_avatar(value, base_dir: str | Path, who: str) -> dict | None:
    """{"kind": "file", "path", "type"} | {"kind": "url", "url"} | {"kind": "text", "text"} | None."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"{who}: avatar must be an image path, an http(s) URL or a short text/emoji")
    value = value.strip()
    if URL.match(value):
        return {"kind": "url", "url": value}
    suffix = Path(value).suffix.lower()
    if suffix or "/" in value or "\\" in value:
        if suffix not in TYPES:
            raise ValueError(f"{who}: avatar {value!r} must be one of {', '.join(sorted(TYPES))}")
        p = Path(value).expanduser()
        p = p if p.is_absolute() else Path(base_dir) / p
        if not p.is_file():
            raise ValueError(f"{who}: avatar file {p} not found")
        if p.stat().st_size > MAX_BYTES:
            raise ValueError(f"{who}: avatar file {p.name} is larger than 2 MB")
        return {"kind": "file", "path": str(p.resolve()), "type": TYPES[suffix]}
    if len(value) > MAX_TEXT * 2 or len(value.encode("utf-16-le")) // 2 > MAX_TEXT * 4:
        raise ValueError(f"{who}: avatar text {value!r} is too long (an emoji or up to {MAX_TEXT} letters)")
    return {"kind": "text", "text": value}


def folder_for(cfg: dict, base_dir: str | Path) -> Path | None:
    """The drop-in avatars folder from agents.json ("avatars_dir"): default `avatars/` next to it, false = off."""
    v = cfg.get("avatars_dir", "avatars")
    if v is False or v is None:
        return None
    if not isinstance(v, str) or not v.strip():
        raise ValueError('"avatars_dir" must be a folder name (relative to agents.json) or false')
    p = Path(v.strip()).expanduser()
    return p if p.is_absolute() else Path(base_dir) / p


def from_folder(folder: Path | None, name: str) -> dict | None:
    """The image file `<name>.<ext>` in `folder` as an avatar, or None. Only a plain file that really lives in the
    folder counts (no links out of it), of an allowed type and size; anything else is ignored, not an error."""
    if folder is None or not folder.is_dir() or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", name):
        return None
    root = folder.resolve()
    for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"):
        for cand in (folder / f"{name}{ext}", folder / f"{name}{ext.upper()}"):
            try:
                if cand.is_file() and cand.resolve().parent == root and 0 < cand.stat().st_size <= MAX_BYTES:
                    return {"kind": "file", "path": str(cand.resolve()), "type": TYPES[ext]}
            except OSError:
                continue
    return None


def public(name: str, info: dict | None) -> dict | None:
    """What the dashboard gets: never a local path."""
    if not info:
        return None
    if info["kind"] == "file":
        v = int(Path(info["path"]).stat().st_mtime) if Path(info["path"]).exists() else 0
        return {"kind": "image", "url": f"/api/avatars/{name}?v={v}"}
    if info["kind"] == "url":
        return {"kind": "image", "url": info["url"]}
    return {"kind": "text", "text": info["text"]}
