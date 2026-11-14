"""Fenced code block parsing with file-name detection.

Recognised file-name conventions (first match wins):

Info string
    ```python src/app.py            lang + path
    ```src/app.py                   path only (lang from extension)
    ```python:src/app.py            lang:path   (also lang|path)
    ```python title="src/app.py"    key=value   (file, filename, path, filepath, title, name, src)
    ```{.python file="src/app.py"}  pandoc / attribute style
    ```python (src/app.py)          bracketed / quoted path
Line just before the fence (one blank line allowed)
    **src/app.py**   `src/app.py`   ### src/app.py   src/app.py:
    File: src/app.py   Filename: `src/app.py`   Path - src/app.py
    Create `src/app.py` with:  (first back-ticked path in a short sentence)
First line inside the block
    # file: src/app.py   // filename: src/app.ts   <!-- path: index.html -->   /* file: a.css */
    -- file: q.sql   ; file: x.ini   % file: x.tex   or a bare comment that is a path: # src/app.py
"""
from __future__ import annotations

import posixpath
import re
import shlex
from dataclasses import dataclass

EXT = {
    "python": "py", "py": "py", "python3": "py", "javascript": "js", "js": "js", "typescript": "ts",
    "ts": "ts", "tsx": "tsx", "jsx": "jsx", "json": "json", "jsonc": "json", "yaml": "yaml", "yml": "yaml",
    "toml": "toml", "bash": "sh", "sh": "sh", "shell": "sh", "zsh": "sh", "console": "txt",
    "powershell": "ps1", "ps1": "ps1", "bat": "bat", "html": "html", "css": "css", "scss": "scss",
    "sql": "sql", "markdown": "md", "md": "md", "go": "go", "rust": "rs", "rs": "rs", "java": "java",
    "c": "c", "h": "h", "cpp": "cpp", "c++": "cpp", "hpp": "hpp", "csharp": "cs", "cs": "cs",
    "ruby": "rb", "rb": "rb", "php": "php", "xml": "xml", "svg": "svg", "dockerfile": "Dockerfile",
    "makefile": "mk", "make": "mk", "text": "txt", "txt": "txt", "plaintext": "txt", "csv": "csv",
    "ini": "ini", "kotlin": "kt", "kt": "kt", "swift": "swift", "r": "r", "mermaid": "mmd",
    "vue": "vue", "svelte": "svelte", "lua": "lua", "perl": "pl", "scala": "scala", "dart": "dart",
    "diff": "diff", "patch": "patch", "graphql": "graphql", "proto": "proto", "tf": "tf", "hcl": "hcl",
    "nginx": "conf", "env": "env", "dotenv": "env", "elixir": "ex", "haskell": "hs", "zig": "zig",
}
LANG_FROM_EXT = {v: k for k, v in reversed(list(EXT.items()))}
# File names without an extension that are still clearly files.
BARE_NAMES = {"Dockerfile", "Makefile", "Procfile", "Gemfile", "Rakefile", "Vagrantfile", "Jenkinsfile",
              "LICENSE", "README", "CHANGELOG", "CODEOWNERS", "Containerfile", "Brewfile", "Justfile"}
NAME_KEYS = {"file", "filename", "file_name", "path", "filepath", "file_path", "title", "name", "src", "source"}

OPEN_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")
PATH_CHARS = re.compile(r"^[\w@+~.\-/\\ \[\]()]+$")
EXT_RE = re.compile(r"\.[A-Za-z0-9_+\-]{1,12}$")
TICKED = re.compile(r"`([^`\n]+)`")
PREFIX_RE = re.compile(
    r"^(?:#{1,6}\s*)?(?:[-*+]\s+|\d+[.)]\s+)?(?:\*\*|__)?\s*"
    r"(?:(?:new|updated|modified|create|created|update|edit|edited|changed|full|complete|final)\s+)?"
    r"(?:file(?:\s*name)?|filename|path|file\s*path|location|in|to|for)?\s*(?:\*\*|__)?\s*[:\-=–—]?\s*",
    re.I)
COMMENT_RE = re.compile(
    r"^\s*(?:#|//|--|;|%|'|/\*|<!--|\{-|\(\*|rem\s)\s*"
    r"(?P<key>(?:file\s*name|filename|file|path|filepath)\s*[:=]\s*)?"
    r"(?P<path>[^\s*]+?)\s*(?:\*/|-->|-}|\*\))?\s*$", re.I)


@dataclass
class Fence:
    lang: str           # normalised language ('' if none)
    info: str           # raw info string
    code: str           # block content
    path: str | None    # declared relative file path, normalised; None if unnamed
    source: str | None  # where the path came from: info|attr|before|comment
    closed: bool        # False if the reply ended before the closing fence (truncated)
    start: int          # line index of the opening fence
    end: int            # line index of the closing fence


def normalize_path(p: str | None) -> str | None:
    """Make a declared path safe and relative. Returns None if it is unusable."""
    if not p:
        return None
    p = p.strip().strip("`'\"*").strip().rstrip(":,;").strip("`'\"*")
    p = p.replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", p):
        p = p[3:]
    if re.match(r"^[a-z]+://", p, re.I):  # URL, not a file
        return None
    p = p.lstrip("/")
    while p.startswith("./"):
        p = p[2:]
    norm = posixpath.normpath(p) if p else ""
    if not norm or norm == "." or norm.startswith("..") or "/../" in f"/{norm}/":
        return None
    if len(norm) > 240 or any(len(part) > 120 for part in norm.split("/")):
        return None
    return norm


def looks_like_path(s: str, allow_spaces: bool = False) -> bool:
    s = s.strip().strip("`'\"*").rstrip(":")
    if not s or len(s) > 240 or s.startswith(("http://", "https://", "-")):
        return False
    if " " in s and not allow_spaces:
        return False
    s = re.sub(r"^[A-Za-z]:(?=[\\/])", "", s)  # Windows drive letter
    if not PATH_CHARS.match(s):
        return False
    base = s.replace("\\", "/").rstrip("/").split("/")[-1]
    if not base:
        return False
    if base in BARE_NAMES or (base.startswith(".") and len(base) > 1 and "." not in base[1:]):
        return True  # Dockerfile, .gitignore, .env
    if EXT_RE.search(base) and not re.fullmatch(r"\d+(\.\d+)+", base):  # not a version number
        return True
    return "/" in s.replace("\\", "/").strip("/") and not s.endswith("/")


def lang_for(path: str | None, lang: str) -> str:
    if lang:
        return lang
    if not path:
        return ""
    base = path.split("/")[-1]
    if base in BARE_NAMES:
        return base.lower()
    m = EXT_RE.search(base)
    return LANG_FROM_EXT.get(m.group(0)[1:].lower(), m.group(0)[1:].lower()) if m else ""


def _split(info: str) -> list[str]:
    if "\\" in info:  # Windows paths: shlex would eat the backslashes
        return [t.strip("'\"") for t in info.split()]
    try:
        return shlex.split(info)
    except ValueError:
        return info.split()


def parse_info(info: str) -> tuple[str, str | None, str | None]:
    """Return (lang, path, source) from a fence info string."""
    raw = info.strip()
    if not raw:
        return "", None, None
    attr = raw.startswith("{") and raw.endswith("}")
    body = raw[1:-1].strip() if attr else raw
    tokens = _split(body)
    lang, path, src = "", None, None
    rest = []
    for t in tokens:
        if "=" in t:
            k, v = t.split("=", 1)
            if k.strip().lower().lstrip(".") in NAME_KEYS and v.strip("'\""):
                path, src = path or v.strip("'\""), src or "attr"
            continue
        if t.startswith(".") and attr:
            lang = lang or t[1:]
            continue
        rest.append(t)
    if rest:
        first = rest[0]
        m = re.match(r"^([A-Za-z0-9_+#.\-]+)[:|](.+)$", first)
        if m and not re.match(r"^[A-Za-z]$", m.group(1)) and looks_like_path(m.group(2)):
            lang, path, src = lang or m.group(1), path or m.group(2), src or "info"
        elif looks_like_path(first) and ("/" in first or "." in first.lstrip(".") or first in BARE_NAMES
                                         or first.startswith(".")) and first.lower() not in EXT:
            path, src = path or first, src or "info"
        else:
            lang = lang or first
            for t in rest[1:]:
                t2 = t.strip("()[]<>")
                if looks_like_path(t2):
                    path, src = path or t2, src or "info"
                    break
    lang = lang.lower().lstrip(".")
    return lang, path, src


def path_from_line(line: str) -> str | None:
    """Detect a file name on the line that introduces a fence."""
    s = line.strip()
    if not s or len(s) > 200 or s.startswith(("```", "~~~")):
        return None
    ticked = [t for t in TICKED.findall(s) if looks_like_path(t)]
    if ticked:
        return ticked[0]
    stripped = PREFIX_RE.sub("", s, count=1).strip().strip("*_").strip().rstrip(":").strip()
    stripped = re.sub(r"\s*\((?:new|updated|modified|excerpt|partial)[^)]*\)$", "", stripped, flags=re.I)
    if looks_like_path(stripped):
        return stripped
    return None


def path_from_comment(first: str, lang: str) -> str | None:
    m = COMMENT_RE.match(first)
    if not m:
        return None
    p = m.group("path")
    if not looks_like_path(p):
        return None
    if m.group("key"):
        return p
    # bare comment: only trust it if it has a directory or its extension fits the block's language
    ext = EXT_RE.search(p.split("/")[-1])
    if "/" in p or (ext and lang and EXT.get(lang, lang) == ext.group(0)[1:].lower()):
        return p
    return None


def parse_fences(text: str) -> list[Fence]:
    lines = (text or "").split("\n")
    out: list[Fence] = []
    i = 0
    while i < len(lines):
        m = OPEN_RE.match(lines[i])
        if not m or (m.group(2)[0] == "`" and "`" in m.group(3)):
            i += 1
            continue
        indent, fence, info = len(m.group(1)), m.group(2), m.group(3).strip()
        close = re.compile(rf"^ {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}\s*$")
        j = i + 1
        while j < len(lines) and not close.match(lines[j]):
            j += 1
        body = [l[indent:] if l[:indent].strip() == "" else l.lstrip() for l in lines[i + 1:j]]
        code = "\n".join(body) + ("\n" if body else "")
        lang, path, src = parse_info(info)
        if not path:
            k = i - 1
            if k >= 0 and not lines[k].strip() and k - 1 >= 0:
                k -= 1
            if k >= 0 and (k == i - 1 or lines[k].strip()):
                p = path_from_line(lines[k])
                if p:
                    path, src = p, "before"
        if not path and body:
            p = path_from_comment(body[0], lang)
            if p:
                path, src = p, "comment"
        npath = normalize_path(path)
        out.append(Fence(lang=lang_for(npath, lang), info=info, code=code, path=npath,
                         source=src if npath else None, closed=j < len(lines), start=i, end=j))
        i = j + 1
    return out
