"""Skill packs: reusable instructions (and optional scripts) that agents load when a task needs them.

One folder per pack, in the Agent Skills layout:
    skills/<name>/SKILL.md        frontmatter (name, description) + the instructions
    skills/<name>/reference/...   optional extra files the instructions point to
    skills/<name>/scripts/...     optional scripts; run_code finds them at .skills/<name>/scripts/...

Where packs come from: the "skills_dir" of agents.json (default: skills/ next to it; XQO_SKILLS_DIR wins) for
everyone, and a department file's "skills_dir" (relative to the file) for that department's members only.
Who gets them: "skill_packs" per agent, "default_skill_packs" per department and in agents.json (grants.py).

An agent's system prompt lists only each granted pack's name and description; load_skill returns the
instructions and the list of files, read_skill_file returns one file (progressive disclosure).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_DESCRIPTION = 1024
MAX_FILE_BYTES = 256_000  # one file returned by read_skill_file
MAX_FILES_LISTED = 200
SKILL_FILE = "SKILL.md"
RUN_DIR = ".skills"  # where a run copy finds the granted packs

LOAD_SKILL_TOOL = {
    "name": "load_skill",
    "description": "Load one of your skill packs: returns its instructions and the files it has. Load a pack "
                   "before doing work it covers, then follow its instructions.",
    "input_schema": {"type": "object", "properties": {"name": {"type": "string", "description": "The pack's name."}},
                     "required": ["name"], "additionalProperties": False},
}
READ_SKILL_FILE_TOOL = {
    "name": "read_skill_file",
    "description": "Read a file of one of your skill packs (a path that load_skill listed, e.g. reference/api.md).",
    "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "path": {"type": "string"}},
                     "required": ["name", "path"], "additionalProperties": False},
}
SKILL_TOOLS = [LOAD_SKILL_TOOL, READ_SKILL_FILE_TOOL]


@dataclass
class SkillPack:
    name: str
    description: str
    path: Path  # the pack's folder
    department: str | None = None  # None = available to everyone
    meta: dict = field(default_factory=dict)

    def body(self) -> str:
        return split_frontmatter((self.path / SKILL_FILE).read_text(encoding="utf-8"))[1].strip()

    def files(self) -> list[str]:
        out = []
        for p in sorted(self.path.rglob("*")):
            if p.is_file() and not p.is_symlink() and p.name != SKILL_FILE and not any(
                    part.startswith(".") for part in p.relative_to(self.path).parts):
                out.append(p.relative_to(self.path).as_posix())
                if len(out) >= MAX_FILES_LISTED:
                    break
        return out

    def read(self, rel: str) -> str:
        root = self.path.resolve()
        p = (root / rel).resolve()
        if root not in p.parents or not p.is_file():
            raise ValueError(f"skill {self.name!r} has no file {rel!r}")
        if p.stat().st_size > MAX_FILE_BYTES:
            raise ValueError(f"{rel} is larger than {MAX_FILE_BYTES // 1000} KB")
        try:
            return p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise ValueError(f"{rel} is not a text file; scripts run with run_code from {RUN_DIR}/{self.name}/") from None

    def describe(self) -> dict:
        return {"name": self.name, "description": self.description, "department": self.department,
                "path": str(self.path), "files": len(self.files())}


def split_frontmatter(text: str) -> tuple[dict, str]:
    """A tiny reader for the --- key: value --- block (strings, and > / | folded or literal values)."""
    text = text.lstrip("﻿")
    if not text.startswith("---"):
        return {}, text
    end = re.search(r"^---\s*$", text[3:], re.M)
    if not end:
        return {}, text
    block, body = text[3:3 + end.start()], text[3 + end.end():]
    meta: dict = {}
    key, folded, lines = None, None, []

    def flush():
        if key is not None and folded is not None:
            meta[key] = (" " if folded == ">" else "\n").join(x.strip() for x in lines).strip()

    for raw in block.splitlines():
        if key is not None and folded is not None and (raw.startswith((" ", "\t")) or not raw.strip()):
            lines.append(raw)
            continue
        flush()
        key, folded, lines = None, None, []
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", raw)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if val in (">", "|", ">-", "|-"):
            folded = val[0]
            continue
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        meta[key] = val
    flush()
    return meta, body


def load_pack(folder: Path, department: str | None = None) -> SkillPack:
    f = folder / SKILL_FILE
    meta, _ = split_frontmatter(f.read_text(encoding="utf-8"))
    name = str(meta.get("name") or folder.name).strip()
    desc = str(meta.get("description") or "").strip()
    if not NAME_RE.match(name):
        raise ValueError(f"{f}: name {name!r} must be lowercase letters, digits and - (at most 64)")
    if not desc:
        raise ValueError(f"{f}: a description is required (it tells agents when to load the pack)")
    if len(desc) > MAX_DESCRIPTION:
        raise ValueError(f"{f}: description is longer than {MAX_DESCRIPTION} characters")
    return SkillPack(name, desc, folder.resolve(), department, meta)


def load_dir(folder: Path, department: str | None = None) -> list[SkillPack]:
    if not folder.is_dir():
        return []
    return [load_pack(d, department) for d in sorted(folder.iterdir())
            if d.is_dir() and not d.name.startswith(".") and (d / SKILL_FILE).is_file()]


def resolve_dir(config_dir: Path, cfg: dict) -> Path:
    v = os.getenv("XQO_SKILLS_DIR") or cfg.get("skills_dir") or "skills"
    p = Path(v).expanduser()
    return p if p.is_absolute() else config_dir / p


class SkillLibrary:
    """Every pack this instance knows: the global ones and each department's own."""

    def __init__(self, packs: list[SkillPack], folders: list[Path]):
        self.packs = {}
        self.folders = folders
        for p in packs:
            if p.name in self.packs:
                raise ValueError(f"skill pack {p.name!r} exists twice: {self.packs[p.name].path} and {p.path}")
            self.packs[p.name] = p

    @classmethod
    def load(cls, config_dir: Path, cfg: dict, dept_files: list[dict]) -> "SkillLibrary":
        folders = [resolve_dir(config_dir, cfg)]
        packs = load_dir(folders[0])
        for d in dept_files:
            if d.get("skills_dir"):
                base = Path(d["_source"]).parent if d.get("_source") else config_dir
                p = Path(d["skills_dir"]).expanduser()
                p = p if p.is_absolute() else base / p
                folders.append(p)
                packs += load_dir(p, d["name"])
        return cls(packs, folders)

    def available(self, department: str) -> dict[str, SkillPack]:
        return {n: p for n, p in self.packs.items() if p.department in (None, department)}

    def describe(self) -> list[dict]:
        return [p.describe() for p in self.packs.values()]


def prompt_section(packs: list[SkillPack]) -> str:
    if not packs:
        return ""
    lines = "\n".join(f"- {p.name}: {p.description}" for p in packs)
    return ("Skill packs you can load (load_skill name) before doing work they cover; follow the loaded "
            f"instructions. Scripts of a pack run with run_code from {RUN_DIR}/<name>/:\n{lines}\n\n")
