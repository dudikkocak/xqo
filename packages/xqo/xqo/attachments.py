"""User attachments on tasks: images, PDFs and text files, sent to Claude as content blocks, or as file
references (``refs``) to agents that read them on demand with Claude Code's Read tool.

Stored under ``artifacts/<root_task>/attachments/<name>``; task events carry only the metadata.
"""
from __future__ import annotations

import base64
import mimetypes
import re
import shutil
from pathlib import Path

# defaults; "limits" in agents.json changes them (limits.apply_limits)
MAX_FILES = 20
MAX_BYTES = {"image": 5_000_000, "pdf": 20_000_000, "text": 2_000_000}
MAX_TOTAL = 24_000_000  # stays under the API request limit once base64-encoded
SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_name(name: str) -> str:
    base = SAFE.sub("_", Path(name.replace("\\", "/")).name).strip("._") or "attachment"
    return base[-120:]


def classify(name: str, media_type: str | None, data: bytes) -> tuple[str, str]:
    """Return (kind, media_type) with kind in image|pdf|text, or raise ValueError."""
    mt = (media_type or "").split(";")[0].strip().lower() or (mimetypes.guess_type(name)[0] or "")
    if mt == "image/jpg":
        mt = "image/jpeg"
    if data[:4] == b"%PDF" or mt == "application/pdf":
        return "pdf", "application/pdf"
    sniffed = _sniff_image(data)  # trust the bytes over the declared type
    if sniffed:
        return "image", sniffed
    if mt.startswith("image/"):
        raise ValueError(f"{name}: unsupported image type {mt} (use PNG, JPEG, GIF or WebP)")
    if b"\x00" not in data[:8192]:
        try:
            data.decode("utf-8")
            return "text", "text/plain"
        except UnicodeDecodeError:
            pass
    raise ValueError(f"{name}: unsupported file type {mt or 'binary'} (images, PDFs and text files work)")


def _sniff_image(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def validate(items: list[tuple[str, str | None, bytes]]) -> list[dict]:
    """Check a batch before it is stored. Returns [{name, media_type, kind, data}]."""
    if len(items) > MAX_FILES:
        raise ValueError(f"at most {MAX_FILES} attachments per task")
    out, total = [], 0
    for name, mt, data in items:
        kind, mt = classify(name, mt, data)
        if not data:
            raise ValueError(f"{name}: file is empty")
        if len(data) > MAX_BYTES[kind]:
            raise ValueError(f"{name}: {kind} files are limited to {MAX_BYTES[kind] / 1_000_000:g} MB")
        total += len(data)
        out.append({"name": name, "media_type": mt, "kind": kind, "data": data})
    if total > MAX_TOTAL:
        raise ValueError(f"attachments total {total / 1_000_000:.1f} MB; the limit is {MAX_TOTAL / 1_000_000:g} MB")
    return out


class AttachmentStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def folder(self, root_id: str) -> Path:
        return self.root / SAFE.sub("_", root_id) / "attachments"

    def save(self, root_id: str, items: list[dict]) -> list[dict]:
        """Store validated items; returns metadata (no bytes) with unique names."""
        folder = self.folder(root_id)
        folder.mkdir(parents=True, exist_ok=True)
        metas = []
        for it in items:
            name = safe_name(it["name"])
            stem, dot, ext = name.rpartition(".") if "." in name else (name, "", "")
            n = 1
            while (folder / name).exists():
                n += 1
                name = f"{stem}-{n}{dot}{ext}"
            (folder / name).write_bytes(it["data"])
            metas.append({"name": name, "media_type": it["media_type"], "kind": it["kind"], "bytes": len(it["data"])})
        return metas

    def path(self, root_id: str, name: str) -> Path | None:
        p = self.folder(root_id) / safe_name(name)
        return p if p.is_file() else None

    def copy(self, src_root: str, dst_root: str) -> None:
        src = self.folder(src_root)
        if src.is_dir():
            shutil.copytree(src, self.folder(dst_root), dirs_exist_ok=True)

    def blocks(self, root_id: str, metas: list[dict]) -> list[dict]:
        """Anthropic content blocks for the given attachments (missing files are skipped)."""
        out = []
        for m in metas:
            p = self.path(root_id, m["name"])
            if not p:
                continue
            data = p.read_bytes()
            if m["kind"] == "image":
                out.append({"type": "image", "source": {"type": "base64", "media_type": m["media_type"],
                                                        "data": base64.b64encode(data).decode()}})
            elif m["kind"] == "pdf":
                out.append({"type": "document", "title": m["name"],
                            "source": {"type": "base64", "media_type": "application/pdf",
                                       "data": base64.b64encode(data).decode()}})
            else:
                out.append({"type": "document", "title": m["name"],
                            "source": {"type": "text", "media_type": "text/plain",
                                       "data": data.decode("utf-8", errors="replace")}})
        return out


    def refs(self, root_id: str, metas: list[dict]) -> str:
        """The attachments as file references (absolute paths) instead of content (missing files are skipped)."""
        lines = [f"- {p.resolve()} ({m['kind']}, {_size(m['bytes'])})" for m in metas if (p := self.path(root_id, m["name"]))]
        if not lines:
            return ""
        return ("Attached files (not included here: open one with the Read tool when you need it; Read shows images "
                "and PDFs too):\n" + "\n".join(lines))


def describe(metas: list[dict]) -> str:
    return ", ".join(f"{m['name']} ({m['kind']}, {_size(m['bytes'])})" for m in metas)


def _size(n: int) -> str:
    return f"{n / 1_000_000:.1f} MB" if n >= 1_000_000 else f"{max(1, n // 1000)} KB"
