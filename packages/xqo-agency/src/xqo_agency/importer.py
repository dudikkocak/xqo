"""Import agents from the agency-agents prompt library and keep them up to date.

Upstream: https://github.com/msitarzewski/agency-agents (MIT). Each agent there is a Markdown file with a short
frontmatter (name, description, emoji, ...) and a long prompt; files are grouped in division folders.

Three files next to agents.json:
    agency.json             what you pick and your own additions (edit this one):
        {"repo": "https://github.com/msitarzewski/agency-agents", "ref": "main",
         "adapter": null,                        # text appended to every prompt (null = built-in, "" = none)
         "departments": {"testing": {"description": "...", "contacts": "*", "budget": {...}, "head": "..."}},
         "agents": {"engineering/engineering-code-reviewer.md":
                        {"department": "engineering", "skills": ["code review", "security"], "can_run_code": false}}}
    agency.lock.json        the upstream commit in use and a hash of each imported file (written by the importer)
    AGENCY_AGENTS_LICENSE   upstream's licence, kept with the copied prompts
An agent entry takes any agent field (name, skills, model, effort, can_run_code, write_paths, mcp, avatar, ...);
they win over what is generated. "department" picks where it goes: an existing department file gets
the agent added next to its hand-written members, a new one is created with a generated head, "hq" = agents.json.

Generated agents carry "_agency": {"path", "commit"} and are rewritten on every sync; hand-written agents and
department settings are never touched. `add`, `remove` and `sync` stay on the locked commit; only `update` moves it.
"""
from __future__ import annotations

import argparse
import difflib
import fnmatch
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from xqo.api import HQ, NAME_RE, PACKAGED_DEPARTMENTS, resolve_avatar, resolve_dir

DEFAULT_REPO = "https://github.com/msitarzewski/agency-agents"
OVERLAY, LOCK, LICENSE = "agency.json", "agency.lock.json", "AGENCY_AGENTS_LICENSE"
SKIP_DIRS = {"integrations", "scripts", "examples", ".github"}
MAX_DOWNLOAD = 64 * 1024 * 1024
ADAPTER = ("You work inside the xqo. Use its tools: handover_task for work that needs another "
           "skill, ask_agent for questions to another agent, the workspace file tools for files "
           "(scope=\"department\" for your department's own workspace) and run_code only if you have it. "
           "Ignore any instructions above that assume tools, memory, integrations or files you do not have, "
           "and end with a concise final answer.")
AGENT_KEYS = {"department"}  # overlay keys that are not agent fields


@dataclass
class Upstream:
    commit: str
    files: dict[str, bytes]  # agent path -> file content
    license: str = ""
    divisions: dict = field(default_factory=dict)


@dataclass
class Plan:
    old_commit: str | None
    upstream: Upstream
    rows: list[tuple[str, str, str, str]] = field(default_factory=list)  # (status, path, agent, detail)
    writes: dict[Path, dict | None] = field(default_factory=dict)  # None = delete the file
    overlay: dict = field(default_factory=dict)
    lock: dict = field(default_factory=dict)
    new_upstream: list[str] = field(default_factory=list)

    @property
    def changes(self) -> list:
        return [r for r in self.rows if r[0] != "unchanged"]


# ---- reading upstream --------------------------------------------------------------------------

def parse_agent(text: str) -> tuple[dict, str] | None:
    """Frontmatter (flat `key: value`, plus `|` / `>` blocks) and body; None when the file is not an agent."""
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        return None
    meta, i = {}, 1
    while i < end:
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", lines[i])
        i += 1
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if val in ("|", ">", "|-", ">-", "|+", ">+"):
            block = []
            while i < end and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
                block.append(lines[i].strip())
                i += 1
            val = ("\n" if val.startswith("|") else " ").join(block).strip()
        elif len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        meta[key] = val
    if not meta.get("name"):
        return None
    return meta, "\n".join(lines[end + 1:]).strip()


def _read_tree(files: dict[str, bytes], commit: str) -> Upstream:
    """Pick the agent files out of a repository snapshot {relative path: bytes}."""
    divisions = {}
    if "divisions.json" in files:
        try:
            divisions = json.loads(files["divisions.json"]).get("divisions") or {}
        except (ValueError, AttributeError):
            divisions = {}
    agents = {}
    for path, data in files.items():
        top = path.split("/", 1)[0]
        if not path.endswith(".md") or "/" not in path or top in SKIP_DIRS or top.startswith("."):
            continue
        if divisions and top not in divisions:
            continue
        if parse_agent(data.decode("utf-8", "replace")):
            agents[path] = data
    lic = next((files[n] for n in ("LICENSE", "LICENSE.md", "LICENSE.txt") if n in files), b"")
    return Upstream(commit, agents, lic.decode("utf-8", "replace"), divisions)


def _snapshot(root: Path) -> dict[str, bytes]:
    out = {}
    for p in root.rglob("*"):
        rel = p.relative_to(root).as_posix()
        if p.is_file() and not rel.startswith(".git/") and (rel.endswith((".md", ".json")) or rel.startswith("LICENSE")):
            out[rel] = p.read_bytes()
    return out


def _git(*args: str, cwd: Path) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    r = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=300)
    if r.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.strip()


def _github(repo: str) -> str | None:
    m = re.match(r"^https://github\.com/([\w.-]+/[\w.-]+?)(?:\.git)?/?$", repo)
    return m.group(1) if m else None


def _download(repo: str, ref: str) -> Upstream:
    """No git: the GitHub tarball (git archive puts the commit id in the pax header)."""
    slug = _github(repo)
    if not slug:
        raise RuntimeError(f"git is not installed and {repo} is not a github.com repository")
    with urllib.request.urlopen(f"https://codeload.github.com/{slug}/tar.gz/{ref}", timeout=120) as r:
        data = r.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD:
        raise RuntimeError("upstream archive is larger than 64 MB")
    files, commit = {}, ref
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        commit = tf.pax_headers.get("comment") or ref
        for m in tf:
            if not m.isfile() or "/" not in m.name:
                continue
            rel = m.name.split("/", 1)[1]
            if rel.endswith((".md", ".json")) or rel.startswith("LICENSE"):
                files[rel] = tf.extractfile(m).read()
    return _read_tree(files, commit)


def fetch(repo: str, ref: str, source: str | None = None) -> Upstream:
    """Upstream at `ref` (branch, tag or commit): a local checkout with --source, else git, else a download."""
    if source:
        root = Path(source).expanduser()
        if not root.is_dir():
            raise RuntimeError(f"--source {root} is not a folder")
        try:
            commit = _git("rev-parse", "HEAD", cwd=root)
        except (RuntimeError, OSError):
            commit = "local"
        return _read_tree(_snapshot(root), commit)
    if not shutil.which("git"):
        return _download(repo, ref)
    with tempfile.TemporaryDirectory(prefix="agency-") as tmp:
        root = Path(tmp)
        _git("init", "-q", cwd=root)
        try:
            _git("fetch", "-q", "--depth", "1", repo, ref, cwd=root)
        except RuntimeError as e:
            raise RuntimeError(f"{e}\n(a commit synced with --source must be read with --source again)") from None
        _git("checkout", "-q", "FETCH_HEAD", cwd=root)
        return _read_tree(_snapshot(root), _git("rev-parse", "HEAD", cwd=root))


# ---- local files ---------------------------------------------------------------------------------

def _load(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"{path.name}: invalid JSON ({e})") from None


def _dump(data) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    s = s if s[:1].isalpha() else f"a_{s}"
    return s[:40].rstrip("_") or "agent"


def short(commit: str | None) -> str:
    return (commit or "none")[:7] if commit and re.fullmatch(r"[0-9a-f]{40}", commit) else (commit or "none")


class Workspace:
    """agents.json, its department files and the importer's own files."""

    def __init__(self, config: str | Path):
        self.config = Path(config)
        if not self.config.is_file():
            raise ValueError(f"{self.config} not found: run `xqo init` first, or pass --config")
        self.base = self.config.parent
        self.cfg = _load(self.config)
        self.overlay_path, self.lock_path = self.base / OVERLAY, self.base / LOCK
        self.depts: dict[str, tuple[Path, dict]] = {}
        self.dept_dir: Path | None = None
        if resolve_dir is not None:
            folder = resolve_dir(self.config, self.cfg)
            self.packaged = folder is not None and Path(folder).resolve() == Path(PACKAGED_DEPARTMENTS).resolve()
            self.dept_dir = self.base / "departments" if self.packaged else Path(folder)
            src = Path(folder) if folder else None
            for p in sorted(src.glob("*.json")) if src and src.is_dir() else []:
                d = _load(p)
                self.depts[d.get("name") or p.stem] = (self.dept_dir / p.name, d)

    def overlay(self, create: bool = False) -> dict:
        ov = _load(self.overlay_path)
        if ov is None:
            if not create:
                raise ValueError(f"{self.overlay_path} not found: start with `xqo agency add <path>`")
            ov = {}
        ov.setdefault("repo", DEFAULT_REPO)
        ov.setdefault("ref", "main")
        ov.setdefault("agents", {})
        return ov

    def lock(self) -> dict:
        return _load(self.lock_path, {}) or {}

    def generated(self) -> dict[str, dict]:
        """Previously imported agents by upstream path, from wherever they were written."""
        out = {}
        for a in self.cfg.get("agents", []):
            if isinstance(a.get("_agency"), dict) and a["_agency"].get("path"):
                out[a["_agency"]["path"]] = a
        for _, d in self.depts.values():
            for a in d.get("agents", []):
                if isinstance(a.get("_agency"), dict) and a["_agency"].get("path"):
                    out[a["_agency"]["path"]] = a
        return out


# ---- planning a sync -----------------------------------------------------------------------------

def _agent(path: str, data: bytes, ov: dict, adapter: str, commit: str) -> dict:
    meta, body = parse_agent(data.decode("utf-8", "replace"))
    a = {"name": slug(meta["name"]), "skills": [meta["name"].lower()],
         "system": body + (f"\n\n{adapter}" if adapter else "")}
    if meta.get("emoji"):
        try:
            if resolve_avatar(meta["emoji"], ".", "agency"):
                a["avatar"] = meta["emoji"]
        except ValueError:
            pass
    a.update({k: v for k, v in ov.items() if k not in AGENT_KEYS})
    a["_agency"] = {"path": path, "commit": commit, "upstream_name": meta["name"]}
    return a


def _head(dept: str, label: str) -> dict:
    return {"name": f"{dept[:35]}_lead", "skills": [f"{label.lower()} management", f"{dept} lead"],
            "system": (f"You lead the {label} department. Split incoming work, assign it to your members with "
                       "handover_task (their skills are listed below), check their results and report back "
                       "concisely."),
            "_agency": {"head": True}}


def _diffstat(old: str, new: str) -> str:
    plus = minus = 0
    for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0):
        if line.startswith("+") and not line.startswith("+++"):
            plus += 1
        elif line.startswith("-") and not line.startswith("---"):
            minus += 1
    return f"+{plus} -{minus}"


def plan(ws: Workspace, up: Upstream, overlay: dict, prune: bool = False) -> Plan:
    lock, previous = ws.lock(), ws.generated()
    pl = Plan(lock.get("commit"), up, overlay=json.loads(json.dumps(overlay)))
    ov_agents = pl.overlay["agents"]
    adapter = ADAPTER if pl.overlay.get("adapter") is None else pl.overlay["adapter"]
    locked = lock.get("files") or {}
    by_hash: dict[str, list[str]] = {}
    for p, data in up.files.items():
        by_hash.setdefault(_sha(data), []).append(p)

    # 1. follow renames, keep or drop agents that vanished upstream
    built: dict[str, dict] = {}
    for path in list(ov_agents):
        if path in up.files:
            continue
        old = locked.get(path, {}).get("sha256")
        moved = [p for p in by_hash.get(old, []) if p not in ov_agents] if old else []
        prev = previous.get(path)
        name = (prev or {}).get("name") or ov_agents[path].get("name") or path
        if len(moved) == 1:
            ov_agents[moved[0]] = ov_agents.pop(path)
            locked = {**locked, moved[0]: locked[path]}
            pl.rows.append(("renamed", f"{path} -> {moved[0]}", name, ""))
        elif prune:
            ov_agents.pop(path)
            pl.rows.append(("removed", path, name, "gone upstream, dropped (--prune)"))
        elif prev:
            built[path] = prev
            pl.rows.append(("missing", path, name, "gone upstream: kept the last imported version (--prune drops it)"))
        else:
            raise ValueError(f"{path} is not in agency-agents at {short(up.commit)} (see `xqo agency list`)")

    # 2. build every selected agent and compare with what is locked
    for path, ov in ov_agents.items():
        if path in built:
            continue
        a = _agent(path, up.files[path], ov, adapter, up.commit)
        built[path] = a
        if any(r[1].endswith(f"-> {path}") for r in pl.rows):
            continue
        prev, sha = previous.get(path), _sha(up.files[path])
        if path not in locked or not prev:
            pl.rows.append(("added", path, a["name"], ""))
        elif locked[path].get("sha256") != sha:
            pl.rows.append(("changed", path, a["name"], _diffstat(prev.get("system", ""), a["system"])))
        else:
            pl.rows.append(("unchanged", path, a["name"], ""))
    seen = {r[1].split(" -> ")[0] for r in pl.rows}
    gone = [p for p in previous if p not in built and p not in seen]
    pl.rows += [("removed", p, previous[p].get("name", p), "no longer in agency.json") for p in gone]

    # 3. place them: agents.json (HQ) or a department file
    targets: dict[str, list[dict]] = {}
    for path, a in built.items():
        dept = (ov_agents.get(path) or {}).get("department") or (path.split("/", 1)[0] if ws.dept_dir else HQ)
        if not ws.dept_dir:
            dept = HQ
        if dept != HQ and not NAME_RE.match(dept):
            raise ValueError(f"{path}: invalid department name {dept!r}")
        targets.setdefault(dept, []).append(a)

    writes: dict[Path, dict | None] = {}
    cfg = dict(ws.cfg)
    cfg["agents"] = [a for a in ws.cfg.get("agents", []) if "_agency" not in a] + targets.pop(HQ, [])
    writes[ws.config] = cfg
    dept_cfg = pl.overlay.get("departments") or {}
    if ws.dept_dir and ws.packaged and targets:  # a local folder replaces the packaged one: copy the examples
        for name, (p, d) in ws.depts.items():
            writes[p] = {k: v for k, v in d.items() if k != "_source"}
    for name, (p, d) in ws.depts.items():
        if isinstance(d.get("_agency"), dict):  # a department this importer created
            if name not in targets:
                writes[p] = None
            continue
        if name in targets or any("_agency" in a for a in d.get("agents", [])):
            writes[p] = {**d, "agents": [a for a in d.get("agents", []) if "_agency" not in a] + targets.pop(name, [])}
    for name, members in targets.items():  # new departments
        p = ws.depts[name][0] if name in ws.depts else ws.dept_dir / f"{name}.json"
        dc = dept_cfg.get(name) or {}
        label = (ws_div := up.divisions.get(name) or {}).get("label") or name.replace("-", " ").title()
        head = dc.get("head")
        agents = list(members)
        if isinstance(head, str) and head not in [a["name"] for a in members]:
            raise ValueError(f"{OVERLAY}: head {head!r} of department {name!r} is not one of its imported agents")
        if not isinstance(head, str):
            h = _head(name, label)
            if isinstance(head, dict):
                h.update(head)
            agents.insert(0, h)
            head = h["name"]
        d = {"name": name, "description": dc.get("description") or f"{label} specialists from agency-agents.",
             "head": head, "contacts": dc.get("contacts", "*")}
        d.update({k: v for k, v in dc.items() if k not in ("head", "description", "contacts")})
        d["agents"] = agents
        d["_agency"] = {"generated": True, "division": name, "color": ws_div.get("color")}
        writes[p] = d
    pl.writes = writes

    # 4. one name per agent across agents.json and every department
    names: dict[str, str] = {}
    for p, d in {**dict(ws.depts.values()), **writes}.items():
        for a in (d or {}).get("agents", []):
            if a["name"] in names:
                raise ValueError(f"agent name {a['name']!r} is used twice ({names[a['name']]} and {p.name}): "
                                 f"set \"name\" for it in {OVERLAY}")
            names[a["name"]] = p.name

    pl.lock = {"repo": pl.overlay["repo"], "ref": pl.overlay["ref"], "commit": up.commit,
               "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "files": {p: {"sha256": _sha(up.files[p]), "name": built[p]["name"]}
                         for p in ov_agents if p in up.files},
               "catalog": sorted(up.files)}
    for p in ov_agents:  # keep the lock entry of a missing agent so a later rename can still be followed
        if p not in up.files and p in locked:
            pl.lock["files"][p] = locked[p]
    if lock.get("catalog"):
        pl.new_upstream = sorted(set(up.files) - set(lock["catalog"]) - set(ov_agents))
    # what the last commit move brought in, for `list --new`
    pl.lock["new"] = pl.new_upstream if up.commit != lock.get("commit") else lock.get("new", [])
    return pl


def apply(ws: Workspace, pl: Plan) -> list[str]:
    """Write everything, then load the result once; on any error put every file back."""
    from xqo.api import build

    backup = {p: (p.read_bytes() if p.exists() else None) for p in [*pl.writes, ws.overlay_path, ws.lock_path,
                                                                    ws.base / LICENSE]}
    written = []
    try:
        for p, d in pl.writes.items():
            if d is None:
                p.unlink(missing_ok=True)
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                if not p.exists() or p.read_text(encoding="utf-8") != _dump(d):
                    p.write_text(_dump(d), encoding="utf-8")
                else:
                    continue
            written.append(str(p.relative_to(ws.base)) if p.is_relative_to(ws.base) else str(p))
        ws.overlay_path.write_text(_dump(pl.overlay), encoding="utf-8")
        ws.lock_path.write_text(_dump(pl.lock), encoding="utf-8")
        if pl.upstream.license:
            (ws.base / LICENSE).write_text(pl.upstream.license, encoding="utf-8")
        with tempfile.TemporaryDirectory(prefix="agency-check-") as tmp:
            kw = {"departments_dir": str(ws.dept_dir)} if ws.dept_dir and ws.dept_dir.is_dir() else {}
            build(ws.config, Path(tmp) / "artifacts", str(Path(tmp) / "events.jsonl"), mock=True, **kw)
    except Exception:
        for p, data in backup.items():
            if data is None:
                p.unlink(missing_ok=True)
            else:
                p.write_bytes(data)
        raise
    return written


# ---- command line ----------------------------------------------------------------------------------

def add_arguments(p: argparse.ArgumentParser) -> None:
    sub = p.add_subparsers(dest="agency_cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--source", help="read upstream from a local checkout instead of fetching it")
    common.add_argument("--reload", action="store_true",
                        help="ask the running server to reload department files afterwards")
    common.add_argument("--url", default=None,
                        help="server address for --reload (XQO_URL, else the config's \"server\" block)")
    ls = sub.add_parser("list", parents=[common], help="list the agents upstream (at the locked commit)")
    ls.add_argument("--division", help="only this division folder, e.g. engineering")
    ls.add_argument("--search", help="only agents whose path, name or description contains this text")
    ls.add_argument("--new", action="store_true", help="only agents added upstream since the last sync")
    ls.add_argument("--ref", help="look at this branch, tag or commit instead of the locked one")
    ad = sub.add_parser("add", parents=[common], help="import agents: paths, globs or division names")
    ad.add_argument("items", nargs="+", metavar="PATH")
    ad.add_argument("--department", help="department to put them in (default: their division, 'hq' = agents.json)")
    ad.add_argument("--skills", help="comma-separated skills (default: the agent's own name)")
    ad.add_argument("--name", help="agent name (one agent only)")
    rm = sub.add_parser("remove", parents=[common], help="drop imported agents (paths, globs or agent names)")
    rm.add_argument("items", nargs="+", metavar="PATH")
    sub.add_parser("sync", parents=[common], help="regenerate from agency.json at the locked commit")
    up = sub.add_parser("update", parents=[common], help="move to the newest upstream commit and show what changed")
    up.add_argument("--ref", help="branch, tag or commit to move to (default: \"ref\" in agency.json)")
    up.add_argument("--dry-run", action="store_true", help="only report, write nothing")
    up.add_argument("--check", action="store_true",
                    help="only report; exit 1 when an imported agent changed (for a scheduled job)")
    up.add_argument("--prune", action="store_true", help="drop imported agents that were deleted upstream")
    sub.add_parser("status", help="show the locked commit and the imported agents (no network)")


def _expand(items: list[str], catalog: list[str]) -> list[str]:
    out = []
    for it in items:
        it = it.strip().strip("/")
        hits = ([it] if it in catalog else
                [p for p in catalog if p.startswith(f"{it}/")] or
                [p for p in catalog if fnmatch.fnmatch(p, it)] or
                [p for p in catalog if p.rsplit("/", 1)[-1] in (it, f"{it}.md")])
        if not hits:
            raise ValueError(f"{it!r} matches no agent upstream (see `xqo agency list`)")
        out += [h for h in hits if h not in out]
    return out


def _print_plan(pl: Plan) -> None:
    print(f"agency-agents {short(pl.old_commit)} -> {short(pl.upstream.commit)}"
          + ("" if pl.old_commit != pl.upstream.commit else " (same commit)"))
    for status, path, name, detail in pl.changes:
        print(f"  {status:<9} {path}  {name}" + (f"  {detail}" if detail else ""))
    same = sum(1 for r in pl.rows if r[0] == "unchanged")
    if same:
        print(f"  unchanged {same} agent(s)")
    if pl.new_upstream:
        print(f"  {len(pl.new_upstream)} new agent(s) upstream: `xqo agency list --new`")


def _reload(a) -> None:
    from xqo.api import client_api as _api
    try:
        r = _api(a.url, "POST", "/api/departments/reload", {})
        print(f"reloaded: {', '.join(r.get('departments', []))}")
    except (RuntimeError, ConnectionError) as e:
        print(f"reload failed ({e}): use ↻ reload on the dashboard or restart")


def main(a) -> None:
    try:
        ws = Workspace(a.config)
        if a.agency_cmd == "status":
            lock, ov = ws.lock(), ws.overlay(create=True)
            print(f"repo {ov['repo']} ref {ov['ref']}, locked at {short(lock.get('commit'))}"
                  + (f" ({lock['synced_at']})" if lock.get("synced_at") else ""))
            for path, o in ov["agents"].items():
                f = (lock.get("files") or {}).get(path, {})
                print(f"  {path}  {f.get('name') or o.get('name') or '?'}"
                      + (f"  -> {o['department']}" if o.get("department") else ""))
            return
        ov = ws.overlay(create=a.agency_cmd in ("add", "list"))
        lock = ws.lock()
        ref = getattr(a, "ref", None) or (ov["ref"] if a.agency_cmd == "update" else lock.get("commit") or ov["ref"])
        if a.agency_cmd == "list":
            up = fetch(ov["repo"], ref, a.source)
            new = set(lock.get("new") or []) | (set(up.files) - set(lock["catalog"]) if lock.get("catalog") else set())
            for p in sorted(up.files):
                meta, _ = parse_agent(up.files[p].decode("utf-8", "replace"))
                text = f"{p} {meta['name']} {meta.get('description', '')}".lower()
                if (a.division and not p.startswith(f"{a.division}/")) or (a.search and a.search.lower() not in text) \
                        or (a.new and p not in new):
                    continue
                mark = "*" if p in ov["agents"] else " "
                print(f"{mark} {p}  {meta['name']} - {meta.get('description', '')[:90]}")
            return
        up = fetch(ov["repo"], ref, a.source)
        if a.agency_cmd == "add":
            paths = _expand(a.items, sorted(up.files))
            if a.name and len(paths) > 1:
                raise ValueError("--name works with one agent only")
            for p in paths:
                e = ov["agents"].setdefault(p, {})
                if a.department:
                    e["department"] = a.department
                if a.skills:
                    e["skills"] = [s.strip() for s in a.skills.split(",") if s.strip()]
                if a.name:
                    e["name"] = a.name
        elif a.agency_cmd == "remove":
            names = {v.get("name"): k for k, v in (lock.get("files") or {}).items()}
            for it in a.items:
                hits = [names[it]] if it in names else [p for p in ov["agents"] if fnmatch.fnmatch(p, it)
                                                        or p == it or p.startswith(f"{it.strip('/')}/")]
                if not hits:
                    raise ValueError(f"{it!r} matches no imported agent (see `xqo agency status`)")
                for h in hits:
                    ov["agents"].pop(h, None)
        pl = plan(ws, up, ov, prune=getattr(a, "prune", False))
        _print_plan(pl)
        if getattr(a, "check", False) or getattr(a, "dry_run", False):
            if getattr(a, "check", False) and any(r[0] in ("changed", "renamed", "missing") for r in pl.rows):
                sys.exit(1)
            return
        written = apply(ws, pl)
        print(f"wrote {', '.join(written) or 'nothing new'}; {OVERLAY}, {LOCK}")
        hq_changed = any(Path(w).name == ws.config.name for w in written)
        if a.reload and ws.dept_dir:
            _reload(a)
            if hq_changed:
                print(f"{ws.config.name} changed: restart the server for HQ agents")
        elif written:
            print("apply: ↻ reload on the dashboard (department files) or restart the server"
                  if ws.dept_dir else "apply: restart the server")
    except (ValueError, RuntimeError, OSError) as e:
        sys.exit(f"agency: {e}")
