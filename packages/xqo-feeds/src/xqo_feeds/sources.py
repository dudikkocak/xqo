"""Feed sources: where events come from. Each source turns what it sees into FeedEvents and hands them to the hub.

  system    CPU, memory, swap, load and disk usage of this machine, every "every" seconds (psutil)
  process   processes whose name or command line matches: how many run, their CPU and memory (psutil)
  service   systemd units: active or not, sub state, restart count (systemctl; Linux)
  file      files added, changed or deleted under some folders (watchfiles, or polling)
  log       new lines of log files that match a pattern; follows rotation, remembers its place over restarts
  journald  the systemd journal, optionally only some units and priorities (journalctl; Linux)
  docker    Docker engine events: a container died, ran out of memory, turned unhealthy... (Docker socket)
  push      events POSTed by scripts, cron jobs, git hooks and CI (`xqo feed emit`), or GitHub / GitLab
            webhooks ("preset": "github" | "gitlab"); needs a token or signing secret from an environment variable

Samples (periodic readings of system / process / service) update the feed's current values and the rules, but are
not logged one by one; everything else is a feed event in the message flow."""
from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import hmac
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from xqo.api import new_id

if TYPE_CHECKING:
    from . import FeedHub

MAX_TEXT = 2000
MAX_FIELDS = 50
NUM = re.compile(r"\b(0x[0-9a-f]+|\d+(\.\d+)?)\b", re.I)
LEVEL_WORDS = (("critical", re.compile(r"\b(CRITICAL|FATAL|PANIC|EMERG)\b", re.I)),
               ("error", re.compile(r"\b(ERROR|ERR|Traceback|Exception)\b")),
               ("warning", re.compile(r"\b(WARN|WARNING)\b", re.I)))


@dataclass
class FeedEvent:
    feed: str
    kind: str = "event"
    severity: str = "info"
    key: str = ""
    fields: dict = field(default_factory=dict)
    text: str = ""
    ts: float = field(default_factory=time.time)
    sample: bool = False       # a periodic reading: updates current values and rules, not logged one by one
    test: bool = False         # made by the dashboard's "test" button
    id: str = field(default_factory=lambda: new_id("fev"))

    def flat(self) -> dict:
        """What rule conditions see: the fields plus feed, kind, severity, key and text."""
        return {**self.fields, "feed": self.feed, "kind": self.kind, "severity": self.severity, "key": self.key,
                "text": self.text}

    def view(self) -> dict:
        return {"id": self.id, "feed": self.feed, "kind": self.kind, "severity": self.severity, "key": self.key,
                "fields": self.fields, "text": self.text, "ts": self.ts, **({"test": True} if self.test else {})}


def flatten(d, prefix: str = "", out: dict | None = None, depth: int = 0) -> dict:
    """Nested JSON to dotted scalar fields (two levels deep, at most MAX_FIELDS, strings capped)."""
    out = {} if out is None else out
    if not isinstance(d, dict):
        return out
    for k, v in d.items():
        if len(out) >= MAX_FIELDS:
            break
        k = re.sub(r"[^A-Za-z0-9_]", "_", str(k))[:40]
        if not k:
            continue
        name = f"{prefix}{k}"
        if isinstance(v, dict) and depth < 2:
            flatten(v, name + ".", out, depth + 1)
        elif isinstance(v, bool) or isinstance(v, (int, float)) or v is None:
            out[name] = v
        elif isinstance(v, str):
            out[name] = v[:500]
        elif isinstance(v, list):
            out[name] = len(v) if any(isinstance(x, (dict, list)) for x in v) else [str(x)[:100] for x in v[:20]]
    return out


def guess_severity(line: str) -> str:
    for sev, rx in LEVEL_WORDS:
        if rx.search(line):
            return sev
    return "info"


def line_key(line: str) -> str:
    """Lines that differ only in numbers, ids and times share a key (for dedupe and digests)."""
    return NUM.sub("#", line.strip())[:200]


def _psutil():
    try:
        import psutil
    except ImportError:
        raise ValueError('needs psutil: pip install "xqo[feeds]" (or pip install psutil)') from None
    return psutil


def _secs(cfg: dict, key: str, default: float, low: float) -> float:
    from xqo_schedules.scheduler import parse_duration
    v = cfg.get(key, default)
    s = parse_duration(v, key) if isinstance(v, str) else float(v)
    if s < low:
        raise ValueError(f"{key} must be at least {low:g}s")
    return s


class Source:
    type = ""
    trust = "untrusted"     # "metrics": numbers and names from this machine; "untrusted": text others can write
    keys: set[str] = set()  # settings this source understands (besides the common ones)
    COMMON = {"type", "trust", "enabled", "max_events_per_minute", "description"}

    def __init__(self, hub: FeedHub, name: str, cfg: dict):
        self.hub, self.name, self.cfg = hub, name, cfg
        bad = set(cfg) - self.COMMON - self.keys
        if bad:
            raise ValueError(f"unknown setting(s) {', '.join(sorted(bad))} for a {self.type} feed")
        if cfg.get("trust") not in (None, "metrics", "untrusted"):
            raise ValueError('trust must be "metrics" or "untrusted"')
        self.trust = cfg.get("trust") or self.trust
        self.error = ""
        self.check()

    def check(self) -> None:
        pass

    async def run(self) -> None:
        """Watch until cancelled. Push sources have nothing to watch."""
        await asyncio.Event().wait()

    def emit(self, **kw) -> FeedEvent:
        ev = FeedEvent(self.name, **kw)
        self.hub.ingest(ev)
        return ev

    def describe(self) -> str:
        return self.type

    def status(self) -> dict:
        return {"type": self.type, "trust": self.trust, "error": self.error, "about": self.describe()}


class Polling(Source):
    every_default, every_min = 30.0, 5.0

    def check(self) -> None:
        self.every = _secs(self.cfg, "every", self.every_default, self.every_min)

    async def run(self) -> None:
        while True:
            await self.poll()
            self.error = ""
            await asyncio.sleep(self.every)

    async def poll(self) -> None:
        raise NotImplementedError


class System(Polling):
    type, trust = "system", "metrics"
    keys = {"every", "disks"}

    def check(self) -> None:
        super().check()
        self.ps = _psutil()
        d = self.cfg.get("disks") or ["/" if os.name != "nt" else os.environ.get("SystemDrive", "C:") + "\\"]
        if isinstance(d, list):
            d = {("root" if p in ("/", "\\") or re.fullmatch(r"[A-Za-z]:\\?", p) else
                  re.sub(r"[^A-Za-z0-9_]", "_", Path(p).name) or "disk"): p for p in d}
        if not isinstance(d, dict) or not d:
            raise ValueError('disks must be a list of paths or {"name": path}')
        self.disks = d
        self.ps.cpu_percent(interval=None)  # the first reading is always 0

    def describe(self) -> str:
        return f"every {self.every:g}s; disks {', '.join(self.disks.values())}"

    def read(self) -> dict:
        ps = self.ps
        vm, sw = ps.virtual_memory(), ps.swap_memory()
        f = {"cpu.percent": round(ps.cpu_percent(interval=None), 1), "mem.percent": round(vm.percent, 1),
             "mem.available_mb": round(vm.available / 2**20), "swap.percent": round(sw.percent, 1),
             "procs": len(ps.pids()), "uptime_h": round((time.time() - ps.boot_time()) / 3600, 1)}
        if hasattr(os, "getloadavg"):
            for k, v in zip(("1", "5", "15"), os.getloadavg()):
                f[f"load.{k}"] = round(v, 2)
        first = True
        for name, path in self.disks.items():
            try:
                u = ps.disk_usage(path)
            except OSError:
                continue
            vals = {"percent": round(u.percent, 1), "free_gb": round(u.free / 2**30, 1)}
            for k, v in vals.items():
                f[f"disk.{name}.{k}"] = v
                if first:
                    f[f"disk.{k}"] = v
            first = False
        return f

    async def poll(self) -> None:
        f = await asyncio.to_thread(self.read)
        self.emit(kind="sample", fields=f, sample=True)


class Process(Polling):
    type, trust = "process", "metrics"
    keys = {"every", "match", "regex"}

    def check(self) -> None:
        super().check()
        self.ps = _psutil()
        m, rx = self.cfg.get("match"), self.cfg.get("regex")
        if not m and not rx:
            raise ValueError('a process feed needs "match" (text in the name or command line) or "regex"')
        self.match = [m] if isinstance(m, str) else list(m or [])
        self.rx = re.compile(rx) if rx else None
        self.was: int | None = None

    def describe(self) -> str:
        return f"{' | '.join(self.match) or self.rx.pattern}, every {self.every:g}s"

    def _hit(self, name: str, cmd: str) -> bool:
        if self.rx:
            return bool(self.rx.search(name) or self.rx.search(cmd))
        return any(m.lower() in name.lower() or m.lower() in cmd.lower() for m in self.match)

    def read(self) -> dict:
        n, cpu, mem, pids = 0, 0.0, 0.0, []
        me = os.getpid()
        for p in self.ps.process_iter(["pid", "name", "cmdline", "memory_info"]):
            try:
                if p.info["pid"] == me:
                    continue
                if not self._hit(p.info["name"] or "", " ".join(p.info["cmdline"] or [])):
                    continue
                n += 1
                cpu += p.cpu_percent(interval=None)
                mem += (p.info["memory_info"].rss if p.info["memory_info"] else 0) / 2**20
                pids.append(p.info["pid"])
            except (self.ps.NoSuchProcess, self.ps.AccessDenied, self.ps.ZombieProcess):
                continue
        return {"running": n, "cpu.percent": round(cpu, 1), "mem.mb": round(mem), "pids": [str(x) for x in pids[:10]]}

    async def poll(self) -> None:
        f = await asyncio.to_thread(self.read)
        self.emit(kind="sample", fields=f, sample=True)
        if self.was is not None and (self.was > 0) != (f["running"] > 0):
            self.emit(kind="started" if f["running"] else "stopped", severity="info" if f["running"] else "error",
                      fields={"running": f["running"], "before": self.was})
        self.was = f["running"]


class Service(Polling):
    type, trust = "service", "metrics"
    keys = {"every", "units"}

    def check(self) -> None:
        super().check()
        u = self.cfg.get("units")
        self.units = [u] if isinstance(u, str) else list(u or [])
        if not self.units or not all(re.fullmatch(r"[\w@.:-]{1,100}", x) for x in self.units):
            raise ValueError('a service feed needs "units": systemd unit names such as "nginx" or "app.service"')
        self.prev: dict[str, str] = {}

    def describe(self) -> str:
        return f"{', '.join(self.units)}, every {self.every:g}s"

    async def poll(self) -> None:
        if not shutil.which("systemctl"):
            raise RuntimeError("systemctl not found: service feeds need systemd (Linux)")
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "show", "--no-pager", "-p", "Id,ActiveState,SubState,NRestarts,Result", *self.units,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), 30)
        if proc.returncode:
            raise RuntimeError(f"systemctl show failed: {err.decode(errors='replace')[:200]}")
        for block, unit in zip(out.decode(errors="replace").strip().split("\n\n"), self.units):
            kv = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
            state = kv.get("ActiveState", "unknown")
            f = {"unit": unit, "active": state == "active", "state": state, "sub": kv.get("SubState", ""),
                 "restarts": int(kv["NRestarts"]) if kv.get("NRestarts", "").isdigit() else None,
                 "result": kv.get("Result", "")}
            self.emit(kind="sample", key=unit, fields=f, sample=True)
            old = self.prev.get(unit)
            if old is not None and old != state:
                self.emit(kind="state", key=unit, severity="error" if state == "failed" else
                          "warning" if state != "active" else "info", fields={**f, "before": old})
            self.prev[unit] = state


class Files(Source):
    type = "file"
    keys = {"paths", "include", "ignore", "poll", "every"}
    IGNORE = [".git/*", "*/.git/*", "*.swp", "*~", "*.tmp", "__pycache__/*", "*/__pycache__/*"]

    def check(self) -> None:
        p = self.cfg.get("paths")
        self.paths = [Path(x).expanduser() for x in ([p] if isinstance(p, str) else list(p or []))]
        if not self.paths:
            raise ValueError('a file feed needs "paths": folders or files to watch')
        inc = self.cfg.get("include") or ["*"]
        self.include = [inc] if isinstance(inc, str) else list(inc)
        self.ignore = self.IGNORE + list(self.cfg.get("ignore") or [])
        self.every = _secs(self.cfg, "every", 5, 1)
        self.poll = bool(self.cfg.get("poll"))
        try:
            import watchfiles  # noqa: F401
        except ImportError:
            self.poll = True

    def describe(self) -> str:
        return f"{', '.join(map(str, self.paths))}{' (polling)' if self.poll else ''}"

    def _rel(self, path: Path) -> str | None:
        for base in self.paths:
            try:
                rel = path.relative_to(base).as_posix() if base.is_dir() else path.name
            except ValueError:
                continue
            if any(fnmatch.fnmatch(rel, g) for g in self.ignore):
                return None
            return rel if any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(path.name, g) for g in self.include) else None
        return None

    def _report(self, change: str, path: Path) -> None:
        rel = self._rel(path)
        if rel is None:
            return
        size = None
        with_size = change != "deleted"
        if with_size:
            try:
                size = path.stat().st_size
            except OSError:
                size = None
        self.emit(kind=change, key=str(path), fields={"path": str(path), "name": path.name, "change": change,
                                                      "size": size})

    async def run(self) -> None:
        missing = [str(p) for p in self.paths if not p.exists()]
        if missing:
            self.error = f"not found: {', '.join(missing)}"
        if self.poll:
            return await self._poll()
        import watchfiles
        names = {watchfiles.Change.added: "added", watchfiles.Change.modified: "modified",
                 watchfiles.Change.deleted: "deleted"}
        async for changes in watchfiles.awatch(*[p for p in self.paths if p.exists()]):
            for ch, path in sorted(changes, key=lambda c: c[1]):
                self._report(names.get(ch, "modified"), Path(path))

    def _scan(self) -> dict[str, tuple[float, int]]:
        out = {}
        for base in self.paths:
            items = [base] if base.is_file() else (base.rglob("*") if base.is_dir() else [])
            for p in items:
                if len(out) >= 20000:
                    return out
                try:
                    if p.is_file():
                        st = p.stat()
                        out[str(p)] = (st.st_mtime, st.st_size)
                except OSError:
                    continue
        return out

    async def _poll(self) -> None:
        seen = await asyncio.to_thread(self._scan)
        while True:
            await asyncio.sleep(self.every)
            now = await asyncio.to_thread(self._scan)
            for p in sorted(set(now) | set(seen)):
                if p not in seen:
                    self._report("added", Path(p))
                elif p not in now:
                    self._report("deleted", Path(p))
                elif now[p] != seen[p]:
                    self._report("modified", Path(p))
            seen = now


class Log(Source):
    type = "log"
    keys = {"path", "pattern", "exclude", "every", "max_line", "from_start", "severity"}
    MAX_READ = 256 * 1024

    def check(self) -> None:
        p = self.cfg.get("path")
        self.globs = [p] if isinstance(p, str) else list(p or [])
        if not self.globs:
            raise ValueError('a log feed needs "path": a file or a glob such as /var/log/app/*.log')
        self.pattern = re.compile(self.cfg["pattern"]) if self.cfg.get("pattern") else None
        self.exclude = re.compile(self.cfg["exclude"]) if self.cfg.get("exclude") else None
        self.every = _secs(self.cfg, "every", 2, 0.5)
        self.max_line = int(self.cfg.get("max_line", 500))
        self.severity = self.cfg.get("severity")

    def describe(self) -> str:
        return ", ".join(self.globs) + (f" ~ /{self.pattern.pattern}/" if self.pattern else "")

    def files(self) -> list[Path]:
        import glob
        out = []
        for g in self.globs:
            out += [Path(x) for x in sorted(glob.glob(os.path.expanduser(g)))]
        return [p for p in out if p.is_file()][:200]

    async def run(self) -> None:
        offsets = self.hub.state.setdefault("offsets", {}).setdefault(self.name, {})
        first = not offsets
        while True:
            files = await asyncio.to_thread(self.files)
            if not files:
                self.error = f"no file matches {', '.join(self.globs)}"
            else:
                self.error = ""
            for p in files:
                await asyncio.to_thread(self._read, p, offsets, first and not self.cfg.get("from_start"))
            first = False
            await asyncio.sleep(self.every)

    def _read(self, p: Path, offsets: dict, skip: bool) -> None:
        try:
            st = p.stat()
        except OSError:
            return
        ino = f"{st.st_dev}:{st.st_ino}"
        o = offsets.get(str(p))
        if o is None:
            pos = st.st_size if skip else 0
        elif o.get("ino") != ino or st.st_size < o.get("pos", 0):
            pos = 0  # rotated or truncated: read the new file from the top
        else:
            pos = o["pos"]
        if pos >= st.st_size:
            offsets[str(p)] = {"ino": ino, "pos": pos}
            return
        with p.open("rb") as f:
            f.seek(pos)
            data = f.read(self.MAX_READ)
        end = data.rfind(b"\n")
        if end < 0 and len(data) < self.MAX_READ:
            offsets[str(p)] = {"ino": ino, "pos": pos}
            return  # half a line: wait for the rest
        chunk = data[: end + 1] if end >= 0 else data
        offsets[str(p)] = {"ino": ino, "pos": pos + len(chunk)}
        self.hub.dirty = True
        for raw in chunk.decode("utf-8", errors="replace").splitlines():
            line = raw.rstrip()
            if not line or (self.pattern and not self.pattern.search(line)) or (self.exclude and self.exclude.search(line)):
                continue
            line = line[: self.max_line]
            self.emit(kind="line", key=line_key(line), severity=self.severity or guess_severity(line), text=line,
                      fields={"path": str(p), "file": p.name})


class Journald(Source):
    type = "journald"
    keys = {"units", "priority", "pattern"}
    PRIO = ["critical", "critical", "critical", "error", "warning", "info", "info", "debug"]

    def check(self) -> None:
        u = self.cfg.get("units")
        self.units = [u] if isinstance(u, str) else list(u or [])
        if not all(re.fullmatch(r"[\w@.:-]{1,100}", x) for x in self.units):
            raise ValueError("units: systemd unit names")
        pr = self.cfg.get("priority")
        names = ["emerg", "alert", "crit", "err", "warning", "notice", "info", "debug"]
        if pr is not None and not (str(pr) in names or str(pr).isdigit() and 0 <= int(pr) <= 7):
            raise ValueError(f"priority must be 0-7 or one of {', '.join(names)}")
        self.priority = str(pr) if pr is not None else None
        self.pattern = re.compile(self.cfg["pattern"]) if self.cfg.get("pattern") else None

    def describe(self) -> str:
        return (", ".join(self.units) or "all units") + (f", priority ≤ {self.priority}" if self.priority else "")

    def argv(self) -> list[str]:
        a = ["journalctl", "-f", "-o", "json", "-n", "0", "--no-pager"]
        for u in self.units:
            a += ["-u", u]
        if self.priority:
            a += ["-p", self.priority]
        return a

    def parse(self, line: str) -> None:
        try:
            d = json.loads(line)
        except ValueError:
            return
        msg = d.get("MESSAGE")
        if isinstance(msg, list):  # binary messages arrive as byte lists
            msg = bytes(x for x in msg if isinstance(x, int)).decode("utf-8", errors="replace")
        msg = str(msg or "")[:MAX_TEXT]
        if self.pattern and not self.pattern.search(msg):
            return
        unit = d.get("_SYSTEMD_UNIT") or d.get("SYSLOG_IDENTIFIER") or ""
        p = int(d["PRIORITY"]) if str(d.get("PRIORITY", "")).isdigit() else 6
        self.emit(kind="journal", key=f"{unit}:{line_key(msg)}", severity=self.PRIO[min(p, 7)], text=msg,
                  fields={"unit": unit, "priority": p, "pid": d.get("_PID"), "host": d.get("_HOSTNAME")})

    async def run(self) -> None:
        if not shutil.which("journalctl"):
            raise RuntimeError("journalctl not found: journald feeds need systemd (Linux)")
        proc = await asyncio.create_subprocess_exec(*self.argv(), stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    err = (await proc.stderr.read()).decode(errors="replace")[:200]
                    raise RuntimeError(f"journalctl stopped{': ' + err if err else ''}")
                self.error = ""
                self.parse(line.decode("utf-8", errors="replace"))
        finally:
            if proc.returncode is None:
                proc.kill()


class Docker(Source):
    type, trust = "docker", "metrics"
    keys = {"socket", "events", "containers"}
    DEFAULT_EVENTS = ["start", "die", "oom", "restart", "health_status"]

    def check(self) -> None:
        host = os.getenv("DOCKER_HOST", "")
        self.socket = self.cfg.get("socket") or (host[7:] if host.startswith("unix://") else "/var/run/docker.sock")
        ev = self.cfg.get("events") or self.DEFAULT_EVENTS
        self.events = [ev] if isinstance(ev, str) else list(ev)
        c = self.cfg.get("containers")
        self.containers = [c] if isinstance(c, str) else list(c or [])

    def describe(self) -> str:
        return f"{', '.join(self.events)} via {self.socket}"

    def parse(self, d: dict) -> None:
        if d.get("Type") != "container":
            return
        action = str(d.get("Action") or d.get("status") or "")
        base, _, detail = action.partition(":")
        if base.strip() not in self.events:
            return
        attrs = (d.get("Actor") or {}).get("Attributes") or {}
        name = attrs.get("name", "")
        if self.containers and not any(fnmatch.fnmatch(name, c) for c in self.containers):
            return
        code = int(attrs["exitCode"]) if str(attrs.get("exitCode", "")).lstrip("-").isdigit() else None
        health = detail.strip() if base == "health_status" else None
        sev = ("critical" if base == "oom" else "error" if (base == "die" and code not in (None, 0)) else
               "warning" if health == "unhealthy" or base == "restart" else "info")
        self.emit(kind=base, key=f"{name}:{base}", severity=sev,
                  fields={"container": name, "image": attrs.get("image", ""), "action": base, "exit_code": code,
                          "health": health})

    async def run(self) -> None:
        import httpx
        if sys.platform == "win32":
            raise RuntimeError("docker feeds need a Unix socket (Linux or macOS)")
        if not Path(self.socket).exists():
            raise RuntimeError(f"Docker socket {self.socket} not found")
        filters = json.dumps({"type": ["container"]})
        transport = httpx.AsyncHTTPTransport(uds=self.socket)
        async with httpx.AsyncClient(transport=transport, timeout=None) as c:
            async with c.stream("GET", "http://docker/events", params={"filters": filters}) as r:
                if r.status_code != 200:
                    raise RuntimeError(f"Docker API answered {r.status_code}")
                self.error = ""
                async for line in r.aiter_lines():
                    if line.strip():
                        try:
                            self.parse(json.loads(line))
                        except ValueError:
                            continue
        raise RuntimeError("the Docker event stream closed")


class Push(Source):
    """Events sent to POST /api/feeds/<name>/events. Generic body: {"kind", "severity", "key", "fields", "text"}
    (or a list of up to 50). Auth: "token_env" (Authorization: Bearer <token>), or "secret_env" (HMAC-SHA256 of
    the body in X-Hub-Signature-256: sha256=<hex>). Presets: "github" (secret_env, the webhook's secret) and
    "gitlab" (token_env, compared with X-Gitlab-Token)."""
    type = "push"
    keys = {"token_env", "secret_env", "preset", "max_body_kb"}
    PRESETS = ("generic", "github", "gitlab")

    def check(self) -> None:
        self.preset = self.cfg.get("preset") or "generic"
        if self.preset not in self.PRESETS:
            raise ValueError(f"preset must be one of {', '.join(self.PRESETS)}")
        self.token_env, self.secret_env = self.cfg.get("token_env"), self.cfg.get("secret_env")
        if self.cfg.get("token") or self.cfg.get("secret"):
            raise ValueError("put the token in an environment variable and name it with token_env / secret_env")
        if self.preset == "github" and not self.secret_env:
            raise ValueError("the github preset needs secret_env (the webhook's secret)")
        if self.preset == "gitlab" and not self.token_env:
            raise ValueError("the gitlab preset needs token_env (the webhook's secret token)")
        if not self.token_env and not self.secret_env:
            raise ValueError("a push feed needs token_env or secret_env: never accept unauthenticated events")
        self.max_body = int(float(self.cfg.get("max_body_kb", 64)) * 1024)

    def describe(self) -> str:
        return f"POST /api/feeds/{self.name}/events" + (f" ({self.preset})" if self.preset != "generic" else "")

    def authorized(self, headers: dict, body: bytes) -> str:
        """"" when the request may come in, else why not."""
        if self.secret_env:
            secret = os.getenv(self.secret_env) or ""
            if not secret:
                return f"{self.secret_env} is not set on the server"
            sig = headers.get("x-hub-signature-256") or headers.get("x-signature-256") or ""
            want = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig, want):
                return "bad or missing signature"
        if self.token_env:
            token = os.getenv(self.token_env) or ""
            if not token:
                return f"{self.token_env} is not set on the server"
            got = headers.get("x-gitlab-token") if self.preset == "gitlab" else \
                (headers.get("authorization") or "").removeprefix("Bearer ").strip() or headers.get("x-feed-token", "")
            if not hmac.compare_digest(got or "", token):
                return "bad or missing token"
        return ""

    def receive(self, headers: dict, body: bytes) -> tuple[int, dict]:
        headers = {k.lower(): v for k, v in headers.items()}
        if len(body) > self.max_body:
            return 413, {"error": f"body larger than {self.max_body // 1024} KB"}
        why = self.authorized(headers, body)
        if why:
            return 401, {"error": why}
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            return 400, {"error": "body is not JSON"}
        if self.preset == "github":
            items = github(headers.get("x-github-event", ""), data)
        elif self.preset == "gitlab":
            items = gitlab(headers.get("x-gitlab-event", ""), data)
        else:
            items = data if isinstance(data, list) else [data]
            if len(items) > 50:
                return 400, {"error": "at most 50 events per request"}
            if not all(isinstance(i, dict) for i in items):
                return 400, {"error": "each event must be an object"}
            items = [generic(i) for i in items]
        ids = [self.emit(**i).id for i in items]
        return 200, {"accepted": len(ids), "ids": ids}


def generic(d: dict) -> dict:
    from .expr import SEVERITIES
    sev = str(d.get("severity") or "info").lower()
    return {"kind": re.sub(r"[^\w.:-]", "_", str(d.get("kind") or "event"))[:40],
            "severity": sev if sev in SEVERITIES else "info", "key": str(d.get("key") or "")[:200],
            "fields": flatten(d.get("fields") or {}), "text": str(d.get("text") or "")[:MAX_TEXT]}


def _fail(conclusion) -> str:
    return "error" if conclusion in ("failure", "timed_out", "startup_failure", "failed", "canceled_by_error") \
        else "warning" if conclusion in ("cancelled", "canceled", "action_required") else "info"


def github(event: str, d: dict) -> list[dict]:
    repo = (d.get("repository") or {}).get("full_name", "")
    if event == "ping" or not event:
        return []
    if event in ("workflow_run", "check_suite", "check_run"):
        r = d.get(event) or {}
        branch = r.get("head_branch") or (r.get("check_suite") or {}).get("head_branch") or ""
        name = (r.get("name") or (r.get("app") or {}).get("name") or "")
        return [{"kind": event, "severity": _fail(r.get("conclusion")), "key": f"{repo}:{name}:{branch}",
                 "text": str(r.get("display_title") or (r.get("head_commit") or {}).get("message") or "")[:500],
                 "fields": {"repo": repo, "name": name, "branch": branch, "action": d.get("action"),
                            "status": r.get("status"), "conclusion": r.get("conclusion"),
                            "url": r.get("html_url"), "sha": (r.get("head_sha") or "")[:12],
                            "actor": (d.get("sender") or {}).get("login")}}]
    if event == "push":
        head = d.get("head_commit") or {}
        branch = str(d.get("ref", "")).removeprefix("refs/heads/")
        return [{"kind": "push", "key": f"{repo}:{branch}", "text": str(head.get("message") or "")[:500],
                 "fields": {"repo": repo, "branch": branch, "commits": len(d.get("commits") or []),
                            "forced": bool(d.get("forced")), "pusher": (d.get("pusher") or {}).get("name"),
                            "sha": str(d.get("after") or "")[:12]}}]
    if event in ("pull_request", "issues"):
        o = d.get("pull_request" if event == "pull_request" else "issue") or {}
        return [{"kind": event, "key": f"{repo}#{o.get('number')}", "text": str(o.get("title") or "")[:500],
                 "fields": {"repo": repo, "action": d.get("action"), "number": o.get("number"),
                            "state": o.get("state"), "merged": o.get("merged"), "user": (o.get("user") or {}).get("login"),
                            "branch": (o.get("head") or {}).get("ref"), "url": o.get("html_url")}}]
    return [{"kind": event[:40], "key": repo, "fields": {"repo": repo, "action": d.get("action")}}]


def gitlab(event: str, d: dict) -> list[dict]:
    kind = d.get("object_kind") or event.lower().replace(" hook", "")
    project = (d.get("project") or {}).get("path_with_namespace", "")
    attrs = d.get("object_attributes") or {}
    if kind == "pipeline":
        return [{"kind": "pipeline", "severity": _fail(attrs.get("status")), "key": f"{project}:{attrs.get('ref')}",
                 "text": str((d.get("commit") or {}).get("message") or "")[:500],
                 "fields": {"project": project, "branch": attrs.get("ref"), "status": attrs.get("status"),
                            "id": attrs.get("id"), "source": attrs.get("source"), "url": attrs.get("url"),
                            "user": (d.get("user") or {}).get("username")}}]
    if kind in ("build", "job"):
        return [{"kind": "job", "severity": _fail(d.get("build_status")), "key": f"{project}:{d.get('build_name')}",
                 "fields": {"project": project or d.get("project_name"), "name": d.get("build_name"),
                            "status": d.get("build_status"), "branch": d.get("ref"), "stage": d.get("build_stage")}}]
    if kind == "push":
        commits = d.get("commits") or []
        branch = str(d.get("ref", "")).removeprefix("refs/heads/")
        return [{"kind": "push", "key": f"{project}:{branch}", "text": str((commits[-1] if commits else {}).get("message") or "")[:500],
                 "fields": {"project": project, "branch": branch, "commits": d.get("total_commits_count", len(commits)),
                            "user": d.get("user_username")}}]
    if kind == "merge_request":
        return [{"kind": "merge_request", "key": f"{project}!{attrs.get('iid')}", "text": str(attrs.get("title") or "")[:500],
                 "fields": {"project": project, "action": attrs.get("action"), "number": attrs.get("iid"),
                            "state": attrs.get("state"), "branch": attrs.get("source_branch"), "url": attrs.get("url")}}]
    return [{"kind": str(kind)[:40], "key": project, "fields": {"project": project}}]


TYPES: dict[str, type[Source]] = {c.type: c for c in (System, Process, Service, Files, Log, Journald, Docker, Push)}
