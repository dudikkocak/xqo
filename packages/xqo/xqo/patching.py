"""Apply model-written patches: unified diffs and SEARCH/REPLACE blocks."""
from __future__ import annotations

import re
from dataclasses import dataclass, field


class PatchError(ValueError):
    pass


# ---------------------------------------------------------------- unified diff
HUNK_RE = re.compile(r"^@@+ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@+")


@dataclass
class FilePatch:
    old_path: str | None  # None for new files (/dev/null)
    new_path: str | None  # None for deletions
    hunks: list[list[str]] = field(default_factory=list)  # raw hunk lines incl. '@@' header


def is_unified_diff(text: str) -> bool:
    return bool(re.search(r"^(?:--- |diff --git )", text, re.M)) and bool(re.search(r"^\+\+\+ ", text, re.M)) \
        and bool(re.search(r"^@@", text, re.M))


def _diff_path(raw: str) -> str | None:
    p = raw.split("\t")[0].strip()
    p = re.sub(r"\s+\d{4}-\d\d-\d\d.*$", "", p)  # drop timestamps
    if p == "/dev/null":
        return None
    if p.startswith(("a/", "b/")):
        p = p[2:]
    return p


def parse_unified(text: str) -> list[FilePatch]:
    lines = text.splitlines()
    patches: list[FilePatch] = []
    cur: FilePatch | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            cur = FilePatch(_diff_path(line[4:]), _diff_path(lines[i + 1][4:]))
            patches.append(cur)
            i += 2
            continue
        if line.startswith("@@") and cur is not None:
            cur.hunks.append([line])
        elif cur is not None and cur.hunks and (line[:1] in (" ", "+", "-", "\\") or line == ""):
            cur.hunks[-1].append(line)
        i += 1
    if not patches:
        raise PatchError("no '--- / +++' file headers found in diff")
    return patches


def _find(haystack: list[str], needle: list[str], hint: int) -> int:
    if not needle:
        return min(max(hint, 0), len(haystack))
    for norm in (lambda s: s, lambda s: s.rstrip(), lambda s: s.strip()):
        n = [norm(x) for x in needle]
        h = [norm(x) for x in haystack]
        hits = [k for k in range(len(h) - len(n) + 1) if h[k:k + len(n)] == n]
        if hits:
            return min(hits, key=lambda k: abs(k - hint))
    return -1


def apply_hunks(original: str, hunks: list[list[str]]) -> str:
    had_nl = original.endswith("\n") or original == ""
    src = original.splitlines()
    offset = 0
    no_newline = False
    for h in hunks:
        m = HUNK_RE.match(h[0])
        hint = (int(m.group(1)) - 1 + offset) if m else 0
        old, new = [], []
        for line in h[1:]:
            if line.startswith("\\"):
                no_newline = True
                continue
            tag, body = (line[:1], line[1:]) if line else (" ", "")
            if tag in (" ", "-"):
                old.append(body)
            if tag in (" ", "+"):
                new.append(body)
        # trailing blank context lines are often dropped/added by models; trim if needed
        at = _find(src, old, hint)
        while at < 0 and old and old[-1].strip() == "" and new and new[-1].strip() == "":
            old, new = old[:-1], new[:-1]
            at = _find(src, old, hint)
        if at < 0:
            preview = "\n".join(old[:6])
            raise PatchError(f"hunk {h[0]!r} does not match the current file near:\n{preview}")
        src[at:at + len(old)] = new
        offset += len(new) - len(old)
    out = "\n".join(src)
    return out + ("\n" if (had_nl and not no_newline and src) else "")


# ---------------------------------------------------------- SEARCH / REPLACE
SR_RE = re.compile(
    r"^(?:(?P<path>[^\n<>=]+?)\s*\n)?<{5,9} ?SEARCH[^\n]*\n(?P<search>.*?)^={5,9}[ \t]*\n(?P<replace>.*?)^>{5,9} ?REPLACE[^\n]*$",
    re.M | re.S)


def is_search_replace(text: str) -> bool:
    return bool(re.search(r"^<{5,9} ?SEARCH", text, re.M)) and bool(re.search(r"^>{5,9} ?REPLACE", text, re.M))


def parse_search_replace(text: str) -> list[tuple[str | None, str, str]]:
    """Return [(path or None, search, replace)]."""
    out = []
    for m in SR_RE.finditer(text):
        path = (m.group("path") or "").strip() or None
        out.append((path, m.group("search"), m.group("replace")))
    if not out:
        raise PatchError("malformed SEARCH/REPLACE block")
    return out


def apply_edit(original: str, old: str, new: str) -> str:
    """Replace exactly one occurrence of old (falls back to whitespace-tolerant line matching)."""
    if old == "":
        return original + new if original else new
    n = original.count(old)
    if n == 1:
        return original.replace(old, new, 1)
    if n > 1:
        raise PatchError(f"search text occurs {n} times; include more context to make it unique")
    src, o = original.splitlines(), old.rstrip("\n").splitlines()
    at = _find(src, o, 0)
    if at < 0:
        raise PatchError(f"search text not found:\n{old[:300]}")
    src[at:at + len(o)] = new.rstrip("\n").splitlines()
    return "\n".join(src) + ("\n" if original.endswith("\n") else "")
