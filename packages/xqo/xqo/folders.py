"""Task folder paths as the user sees them, plus "open in editor" links.

The server only knows its own paths. In Docker they are container paths (/data/...), and on a remote
machine they live on another computer, so the dashboard can't open them directly. `path_map` turns a
server path into the path on the user's side (e.g. the host folder bound to /data), and `ssh_host`
makes editor links open the folder over VS Code Remote-SSH.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import quote

EDITORS = ("vscode", "vscode-insiders", "cursor", "windsurf", "none")


def in_container() -> bool:
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def _parse_map(raw) -> list[tuple[str, str]]:
    """{"/data": "/home/me/xqo/data"} or "/data=/home/me/xqo/data;/x=/y" -> [(server, user)], longest first."""
    if isinstance(raw, str):
        pairs = [p.split("=", 1) for p in raw.split(";") if p.strip()]
        if any(len(p) != 2 for p in pairs):
            raise ValueError(f"folders.path_map: expected 'server_path=user_path', got {raw!r}")
        raw = {a.strip(): b.strip() for a, b in pairs}
    if not isinstance(raw, dict):
        raise ValueError("folders.path_map must be an object {server_path: user_path}")
    rows = [(Path(a).as_posix().rstrip("/") or "/", str(b).rstrip("/\\")) for a, b in raw.items() if a and b]
    return sorted(rows, key=lambda r: -len(r[0]))


@dataclass
class FolderConfig:
    path_map: list[tuple[str, str]] = field(default_factory=list)
    ssh_host: str = ""
    editor: str = "vscode"

    @classmethod
    def from_dict(cls, d: dict | None) -> "FolderConfig":
        d = dict(d or {})
        for key, env in (("path_map", "XQO_PATH_MAP"), ("ssh_host", "XQO_SSH_HOST"), ("editor", "XQO_EDITOR")):
            if env in os.environ:
                d[key] = os.environ[env]
        unknown = set(d) - {"path_map", "ssh_host", "editor"}
        if unknown:
            raise ValueError(f"folders: unknown setting(s) {', '.join(sorted(unknown))}")
        editor = (d.get("editor") or "vscode").lower()
        if editor not in EDITORS:
            raise ValueError(f"folders.editor must be one of {', '.join(EDITORS)}")
        return cls(_parse_map(d.get("path_map") or {}), (d.get("ssh_host") or "").strip(), editor)

    def user_path(self, p: Path) -> str:
        """Server path -> the path the user sees (unchanged when no mapping matches)."""
        s = p.as_posix()
        for src, dst in self.path_map:
            if s == src or s.startswith(src.rstrip("/") + "/"):
                rest = s[len(src):].lstrip("/")
                sep = "\\" if "\\" in dst and "/" not in dst else "/"
                return dst + (sep + rest.replace("/", sep) if rest else "")
        return s

    def editor_uri(self, path: str) -> str | None:
        if self.editor == "none":
            return None
        if self.ssh_host:
            return f"{self.editor}://vscode-remote/ssh-remote+{quote(self.ssh_host, safe='@.-_')}" \
                   f"{quote(PurePosixPath('/', path).as_posix(), safe='/:')}"
        p = path.replace("\\", "/")
        return f"{self.editor}://file{quote(p if p.startswith('/') else '/' + p, safe='/:')}"

    @property
    def mapped(self) -> bool:
        """False when paths are container-internal: copying or opening them on the host won't work."""
        return bool(self.path_map) or bool(self.ssh_host) or not in_container()

    def describe(self, folders: list[tuple[str, Path]]) -> dict:
        mapped = self.mapped
        rows = []
        for kind, p in folders:
            up = self.user_path(p)
            rows.append({"kind": kind, "path": up, "exists": p.is_dir(),
                         "uri": self.editor_uri(up) if mapped else None})
        hint = None if mapped else ("These are paths inside the container. Set XQO_PATH_MAP (e.g. "
                                    "/data=/home/me/xqo/data) to show the host folders and enable editor links.")
        return {"folders": rows, "remote": bool(self.ssh_host), "hint": hint}
