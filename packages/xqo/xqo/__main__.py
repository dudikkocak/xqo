from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

from . import plugin as plugin_mod
from .client import api as _api
from .config import PACKAGED_CONFIG, build, load_config
from .departments import PACKAGED_DEPARTMENTS


async def _run_cli(bus, prompt: str, agent: str | None, files: list[dict] | None = None):
    """Run one task; answer attention requests on the terminal when it is interactive."""
    await bus.mcp.start()
    q = bus.subscribe()
    job = asyncio.create_task(bus.submit(prompt, agent, files))
    interactive = sys.stdin.isatty()
    try:
        while not (job.done() and q.empty()):
            try:
                ev = await asyncio.wait_for(q.get(), 0.5)
            except asyncio.TimeoutError:
                continue
            print(f"{ev['ts'][11:23]}  {ev['kind']:<19} {ev['sender']:>10} -> {ev['recipient'] or '':<10} "
                  f"{ev['content'][:90]!r}")
            if ev["kind"] == "attention_requested":
                m = ev["meta"]
                print(f"\n\a*** ATTENTION ({m['reason']}): {m['question']}\n    options: {m['options'] or '-'}"
                      f"   auto-confirms {m['default']!r} in {int(m['timeout'])}s")
                if interactive:
                    asyncio.create_task(_ask_terminal(bus, m["id"]))
        return await job
    finally:
        bus.unsubscribe(q)
        await bus.mcp.stop()


async def _ask_terminal(bus, att_id: str):
    answer = await asyncio.to_thread(input, "    your answer> ")
    try:
        bus.attention.resolve(att_id, answer.strip() or bus.attention.items[att_id].default)
    except (KeyError, ValueError):
        print("    (already resolved)")


def _manage(a, make_bus) -> None:
    """cancel / delete / restore: through the running server, or (delete/restore) on the data dir directly."""
    try:
        if a.cmd == "cancel":
            for tid in a.ids:
                t = _api(a.url, "POST", f"/api/tasks/{tid}/cancel", {"reason": a.reason})
                print(f"{t['name']}: {t['status']}")
        elif a.cmd == "delete":
            for r in _api(a.url, "POST", "/api/tasks/delete", {"ids": a.ids, "permanent": a.permanent}):
                print(_deleted(r))
        else:
            for tid in a.ids:
                print(f"{_api(a.url, 'POST', f'/api/trash/{tid}/restore')['name']}: restored")
        return
    except RuntimeError as e:
        sys.exit(str(e))
    except ConnectionError:
        if a.cmd == "cancel":
            sys.exit(f"no server answering at {a.url}: a task can only be cancelled in the process running it")
    bus = make_bus()  # no server: work on the data dir
    failed = False
    for tid in a.ids:
        try:
            if a.cmd == "delete":
                print(_deleted({"id": tid, "ok": True, **bus.lifecycle.delete(tid, "cli", a.permanent)}))
            else:
                print(f"{bus.lifecycle.restore(tid, 'cli')['name']}: restored")
        except (KeyError, ValueError) as e:
            failed = True
            print(f"{tid}: {e.args[0] if e.args else e}")
    if failed:
        sys.exit(1)


def _archive(a, make_bus) -> None:
    """archive / unarchive: through the running server, or on the data dir directly."""
    on = a.cmd == "archive"
    try:
        res = _api(a.url, "POST", "/api/tasks/archive", {"ids": a.ids, "archived": on})
    except RuntimeError as e:
        sys.exit(str(e))
    except ConnectionError:
        bus = make_bus()  # no server: work on the data dir
        res = []
        for tid in a.ids:
            try:
                res.append({"ok": True, **(bus.lifecycle.archive(tid, "cli") if on else bus.lifecycle.unarchive(tid, "cli"))})
            except (KeyError, ValueError) as e:
                res.append({"id": tid, "ok": False, "error": e.args[0] if e.args else str(e)})
    for r in res:
        print(f"{r.get('name', r['id'])}: {'archived' if r.get('archived') else 'unarchived'}" if r["ok"]
              else f"{r['id']}: {r['error']}")
    if not all(r["ok"] for r in res):
        sys.exit(1)


def _talk(a) -> None:
    """ask / steer / pause / resume go through the running server."""
    try:
        if a.cmd in ("ask", "steer"):
            r = _api(a.url, "POST", f"/api/tasks/{a.task_id}/chat",
                     {"text": a.text, "kind": a.cmd, "to": a.to, "urgent": a.urgent})
            how = ("reopened the task" if r.get("reopened") else
                   f"answered {r['answered_attention']}" if r.get("answered_attention") else
                   f"queued for {', '.join(r.get('delivered_to') or [])}")
            print(f"{a.cmd} sent ({r['id']}): {how}. Replies show in the dashboard.")
        else:
            t = _api(a.url, "POST", f"/api/tasks/{a.task_id}/{a.cmd}", {} if a.cmd == "pause" else None)
            print(f"{t['name']}: {t['status']}" + (" (reopened)" if t.get("reopened") else ""))
    except RuntimeError as e:
        sys.exit(str(e))
    except ConnectionError:
        sys.exit(f"no server answering at {a.url}")


async def _serve_all(servers) -> None:
    """Dashboard and peer listener in one process; when one stops, the other stops too."""
    jobs = [asyncio.create_task(s.serve()) for s in servers]
    await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
    for s in servers:
        s.should_exit = True
    await asyncio.gather(*jobs, return_exceptions=True)


SERVER_KEYS = {"host", "port", "data_dir", "departments", "mock", "url"}
INIT_SERVER = {"host": "127.0.0.1", "port": 8000, "data_dir": ".", "departments": "all"}


def _server_settings(a) -> None:
    """Fill unset options from the config file's "server" block. Order: command-line flag, environment variable,
    "server" in the config file, built-in default. Relative paths in the file are relative to the file's folder."""
    cfg_path = Path(a.config)
    server = {}
    if cfg_path.is_file():
        try:
            server = load_config(cfg_path).get("server") or {}
        except ValueError as e:
            sys.exit(f"config error: {cfg_path}: {e}")
        bad = set(server) - SERVER_KEYS
        if bad:
            sys.exit(f"config error: unknown \"server\" setting(s) {', '.join(sorted(bad))}; "
                     f"known: {', '.join(sorted(SERVER_KEYS))}")
    base = cfg_path.resolve().parent if cfg_path.is_file() else Path.cwd()

    def pick(flag, env: str, key: str, default):
        if flag is not None:
            return flag
        if os.getenv(env) is not None:
            return os.environ[env]
        return server.get(key, default)

    if a.data_dir is None:
        a.data_dir = os.getenv("XQO_DATA") or (str(base / str(server["data_dir"])) if server.get("data_dir") else ".")
    if a.departments is None and os.getenv("XQO_DEPARTMENTS") is None and "departments" in server:
        d = server["departments"]
        a.departments = ",".join(d) if isinstance(d, list) else str(d)
    a.mock = bool(a.mock or server.get("mock"))
    host = pick(getattr(a, "host", None), "XQO_HOST", "host", "127.0.0.1")
    try:
        port = int(pick(getattr(a, "port", None), "XQO_PORT", "port", 8000))
    except ValueError:
        sys.exit("config error: the port must be a number")
    if hasattr(a, "host"):
        a.host, a.port = host, port
    if hasattr(a, "url") and a.url is None:
        local = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
        a.url = os.getenv("XQO_URL") or server.get("url") or f"http://{local}:{port}"


def _deleted(r: dict) -> str:
    if not r.get("ok"):
        return f"{r['id']}: {r.get('error')}"
    how = "deleted permanently" if r.get("permanent") else "moved to the trash (undo: xqo restore)"
    kept = f"; kept {len(r['branches'])} branch(es) forked from it" if r.get("branches") else ""
    return f"{r.get('name', r['id'])}: {how}{kept}"


def _ops(config: str) -> dict:
    """The "backup" block of agents.json, read without building the instance."""
    try:
        cfg = json.loads(Path(config).read_text(encoding="utf-8")) if Path(config).is_file() else {}
    except ValueError:
        cfg = {}
    b = cfg.get("backup") or {}
    d = b.get("dir")
    if d and not Path(d).expanduser().is_absolute():
        d = str(Path(config).parent / d)
    return {"dir": d, "keep": b.get("keep")}


def _backup(a, make_bus, data: Path) -> None:
    """backup [create] / list / verify / restore."""
    from . import backup
    ops = _ops(a.config)
    folder = Path(getattr(a, "out", None) or ops["dir"] or data / "backups")
    try:
        if a.backup_cmd == "verify":
            m = backup.verify(a.archive)
            print(f"ok: {len(m['files'])} file(s), scope {m['scope']}, made by {m.get('version')} on {m['created']}")
            return
        if a.backup_cmd == "restore":
            r = backup.restore(a.archive, a.to, dry_run=a.dry_run)
            print(("would restore" if a.dry_run else "restored") + f" {r['files']} file(s) ({r['scope']}) into {r['to']}")
            if r["external"]:
                print("files that lived outside the config folder are under _external/: " + ", ".join(r["external"]))
            if r["running_tasks"]:
                print(f"{len(r['running_tasks'])} task(s) were running at backup time: they come back as interrupted")
            if not a.dry_run:
                print("channel sign-ins are not in backups: sign in again (see CHANNELS.md); set the environment "
                      f"variables, then check it: xqo --config {Path(a.to) / 'agents.json'} "
                      f"--data-dir {a.to} check")
                try:  # master config: the restored data sits next to agents.json, so server.data_dir must be "."
                    dd = (load_config(Path(a.to) / "agents.json").get("server") or {}).get("data_dir")
                except (OSError, ValueError):
                    dd = None
                if dd not in (None, ".", "./"):
                    print(f"note: agents.json has \"server\": {{\"data_dir\": {dd!r}}}; the restored data is in "
                          f"{a.to}: set it to \".\" or pass --data-dir {a.to}")
            return
        if a.backup_cmd == "list":
            for p in backup.archives(folder):
                print(f"{p.name}  {p.stat().st_size / 1e6:.1f} MB")
            return
    except (OSError, ValueError, KeyError) as e:
        sys.exit(f"backup: {e}")
    keep = a.keep or ops["keep"]
    if not a.offline:
        try:
            r = _api(a.url, "POST", "/api/admin/backup", {"scope": a.scope, "keep": keep})
            return _print_backup(r)
        except RuntimeError as e:
            sys.exit(str(e))
        except ConnectionError:
            print(f"no server answering at {a.url}: writing the archive directly")
    try:
        r = backup.create(make_bus(), a.scope, folder, data, keep)
    except (OSError, ValueError) as e:
        sys.exit(f"backup: {e}")
    _print_backup(r)


def _print_backup(r: dict) -> None:
    print(f"{r['path']}  ({r['files']} file(s), {r['bytes'] / 1e6:.1f} MB, scope {r['scope']})")
    if r.get("running_tasks"):
        print(f"note: {len(r['running_tasks'])} task(s) were running while it was written")
    for x in r.get("skipped") or []:
        print(f"skipped: {x}")
    if r.get("pruned"):
        print(f"removed {len(r['pruned'])} older archive(s)")


def _unset_env(node, out: set) -> None:
    """Names in *_env settings (keys, tokens) whose environment variable is not set."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k.startswith("_"):  # comments and examples ("_mcp_examples")
                continue
            if k.endswith("_env") and isinstance(v, str) and v and not os.getenv(v):
                out.add(v)
            else:
                _unset_env(v, out)
    elif isinstance(node, list):
        for v in node:
            _unset_env(v, out)


def _check(a, data: Path) -> None:
    """Load the configuration with this code, without starting anything or touching the data."""
    import tempfile

    from . import __version__
    from .config import load_config
    from .migrations import DATA_VERSION, pending, read_data_version
    errors, warnings = [], []
    print(f"xqo {__version__} (data version {DATA_VERSION})")
    with tempfile.TemporaryDirectory() as tmp:
        try:
            bus = build(a.config, Path(tmp) / "artifacts", str(Path(tmp) / "events.jsonl"), mock=True,
                        departments=a.departments, departments_dir=a.departments_dir, run_migrations=False,
                        strict=True)  # a plugin error is an error here, not a warning
        except (ValueError, KeyError, OSError) as e:
            bus = None
            errors.append(f"config: {e}")
        if bus:
            print(f"config: {a.config}: {len(bus.agents)} agent(s), {len(bus.departments)} department(s), "
                  f"{len(bus.skills.packs)} skill pack(s), "
                  f"{len(bus.mcp.servers)} local + {len(bus.mcp_remote)} remote MCP server(s)")
            warnings += list(getattr(bus, "config_warnings", []))
            print(f"storage: {bus.store.describe()}")
            print("plugins: " + ", ".join(f"{n} ({st.state})" for n, st in sorted(bus.plugins.states.items())))
            if a.mcp and bus.mcp.servers:
                async def probe():
                    await bus.mcp.start()
                    st = bus.mcp.status()
                    await bus.mcp.stop()
                    return st
                for st in asyncio.run(probe()):
                    if st["error"]:
                        errors.append(f"MCP {st['name']}: {st['error']}")
                    else:
                        print(f"MCP {st['name']}: {len(st['tools'])} tool(s)")
        missing: set = set()
        try:
            _unset_env(load_config(a.config), missing)
            for p in Path(a.departments_dir or Path(a.config).parent / "departments").glob("*.json"):
                _unset_env(json.loads(p.read_text(encoding="utf-8")), missing)
        except (OSError, ValueError):
            pass
        warnings += [f"environment variable {v} is not set" for v in sorted(missing)]
    root = data / "artifacts"
    have = read_data_version(root) if root.is_dir() else None
    if have is None:
        print(f"data: {root} does not exist yet (a fresh instance)")
    elif have > DATA_VERSION:
        errors.append(f"data: {root} is version {have}, newer than this code ({DATA_VERSION}): "
                      "run the newer version, or restore a backup made before the upgrade")
    else:
        todo = pending(root)
        print(f"data: {root} is version {have}" + (
            f"; at start-up these migrations run: {', '.join(f'v{v} {w}' for v, w in todo)}" if todo else ", up to date"))
    for w in warnings:
        print(f"warning: {w}")
    for e in errors:
        print(f"error: {e}")
    print("check failed" if errors else "check passed")
    if errors:
        sys.exit(1)


def _drain(a) -> None:
    """Stop taking new work, wait for running tasks, optionally stop the server (before an upgrade)."""
    import time
    try:
        if a.off:
            _api(a.url, "POST", "/api/admin/drain", {"on": False})
            print("taking new tasks again")
            return
        st = _api(a.url, "POST", "/api/admin/drain", {"on": True})
        deadline = time.monotonic() + a.timeout
        shown = None
        while st["running_tasks"] and time.monotonic() < deadline:
            names = ", ".join(t["name"] for t in st["running_tasks"])
            if names != shown:
                print(f"waiting for {len(st['running_tasks'])} running task(s): {names}")
                shown = names
            time.sleep(2)
            st = _api(a.url, "GET", "/api/health")
        if st["running_tasks"]:
            print(f"{len(st['running_tasks'])} task(s) still running after {int(a.timeout)}s: after a restart they "
                  "come back as interrupted and can be resumed")
        else:
            print("drained: no task is running")
        if a.stop:
            _api(a.url, "POST", "/api/admin/stop")
            print("server stopping")
        elif not st["running_tasks"]:
            print("safe to stop and upgrade now (or: xqo drain --stop)")
        if st["running_tasks"] and not a.stop:
            sys.exit(2)
    except RuntimeError as e:
        sys.exit(str(e))
    except ConnectionError:
        sys.exit(f"no server answering at {a.url}")


def main() -> None:
    from . import __version__
    p = argparse.ArgumentParser(prog="xqo", description="xqo: plugin-based Claude agents HQ")
    p.add_argument("--version", action="version", version=f"xqo {__version__}")
    p.add_argument("--config", default=os.getenv("XQO_CONFIG", "agents.json"),
                   help="agent roster (falls back to the packaged default)")
    p.add_argument("--data-dir", default=None,
                   help="where artifacts/ and logs/ are written (XQO_DATA, else server.data_dir in the config, else .)")
    p.add_argument("--mock", action="store_true", help="use scripted offline LLM (no API key)")
    p.add_argument("--departments", default=None,
                   help="departments to load, e.g. engineering,marketing ('none' = HQ only; default: all, "
                        "or XQO_DEPARTMENTS)")
    p.add_argument("--departments-dir", default=None,
                   help="folder with one JSON file per department (default: departments/ next to agents.json)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the monitoring website + xqo")
    s.add_argument("--host", default=None, help="XQO_HOST, else server.host in the config, else 127.0.0.1")
    s.add_argument("--port", type=int, default=None, help="XQO_PORT, else server.port in the config, else 8000")
    s.add_argument("--edit", action="store_true", help="allow changing the office layout (the office page's Edit mode); or set \"office\": {\"edit\": true} in agents.json")
    r = sub.add_parser("run", help="run one task from the command line")
    r.add_argument("prompt")
    r.add_argument("--agent")
    r.add_argument("--attach", action="append", default=[], metavar="FILE",
                   help="attach an image, PDF or text file (repeatable)")
    url = None  # XQO_URL, else server.url / host:port from the config file (see _server_settings)
    c = sub.add_parser("cancel", help="cancel running task(s) on the server")
    c.add_argument("ids", nargs="+", metavar="TASK_ID")
    c.add_argument("--reason", default="")
    c.add_argument("--url", default=url, help="server address (XQO_URL)")
    d = sub.add_parser("delete", help="delete finished task(s): to the trash, or --permanent")
    d.add_argument("ids", nargs="+", metavar="TASK_ID")
    d.add_argument("--permanent", action="store_true", help="skip the trash")
    d.add_argument("--url", default=url, help="server address (XQO_URL); without a server the data dir is used")
    u = sub.add_parser("restore", help="bring deleted task(s) back from the trash")
    u.add_argument("ids", nargs="+", metavar="TASK_ID")
    u.add_argument("--url", default=url, help="server address (XQO_URL); without a server the data dir is used")
    for kind, what in (("archive", "archive finished task(s): hidden from the task list, nothing is removed"),
                       ("unarchive", "bring archived task(s) back to the task list")):
        k = sub.add_parser(kind, help=what)
        k.add_argument("ids", nargs="+", metavar="TASK_ID")
        k.add_argument("--url", default=url, help="server address (XQO_URL); without a server the data dir is used")
    for kind, what in (("ask", "ask a running (or finished) task's agents a question"),
                       ("steer", "change a running (or finished) task's instructions")):
        k = sub.add_parser(kind, help=what)
        k.add_argument("task_id")
        k.add_argument("text")
        k.add_argument("--to", help="agent that should receive it (default: the task's entry agent)")
        k.add_argument("--urgent", action="store_true", help="jump the queue; an urgent ask is answered at once")
        k.add_argument("--url", default=url, help="server address (XQO_URL)")
    for kind, what in (("pause", "hold a running task"), ("resume", "continue a paused or finished task")):
        k = sub.add_parser(kind, help=what)
        k.add_argument("task_id")
        k.add_argument("--url", default=url, help="server address (XQO_URL)")
    bk = sub.add_parser("backup", help="back up the configuration and data: create (default), list, verify, restore")
    bk.add_argument("--url", default=url, help="server address (XQO_URL); without a server the data dir is used")
    bk.add_argument("--scope", choices=["full", "state", "config"], default="full",
                    help="full: everything; state: config + runtime settings (schedules, office, channel and feed "
                         "state), no task data; config: the configuration files only")
    bk.add_argument("--keep", type=int, help="keep only the newest N archives of this scope (backup.keep)")
    bk.add_argument("--out", help="folder for the archive when no server runs (default: backup.dir, else <data>/backups)")
    bk.add_argument("--offline", action="store_true", help="write it directly, without asking the server to pause")
    bsub = bk.add_subparsers(dest="backup_cmd")
    bsub.add_parser("create", help="write an archive (the default)")
    bsub.add_parser("list", help="archives in the backup folder")
    bv = bsub.add_parser("verify", help="check an archive against its manifest")
    bv.add_argument("archive")
    br = bsub.add_parser("restore", help="unpack an archive into a new, empty folder")
    br.add_argument("archive")
    br.add_argument("--to", required=True, help="the new instance folder (config files and data dir)")
    br.add_argument("--dry-run", action="store_true", help="verify and show what would be restored")
    ck = sub.add_parser("check", help="load the configuration with this version without starting it "
                                      "(before an upgrade: run it with the new version)")
    ck.add_argument("--mcp", action="store_true", help="also start the local MCP servers once and list their tools")
    dr = sub.add_parser("drain", help="before an upgrade: stop taking new work and wait for running tasks")
    dr.add_argument("--timeout", type=float, default=600, help="seconds to wait for running tasks (default 600)")
    dr.add_argument("--stop", action="store_true", help="then stop the server")
    dr.add_argument("--off", action="store_true", help="cancel a drain: take new work again")
    dr.add_argument("--url", default=url, help="server address (XQO_URL)")
    i = sub.add_parser("init", help="write a starter agents.json to edit")
    i.add_argument("path", nargs="?", default="agents.json")
    plugin_cmds = {}  # commands added by plugins (schedule, channels, feed, peers, agency, …)
    for cls in plugin_mod.load_classes().values():  # a plugin that can't load just adds no command
        try:
            for name in cls.add_cli(sub, url) or []:
                plugin_cmds[name] = cls
        except Exception as e:
            print(f"warning: plugin {cls.name}: its commands are unavailable: {e}", file=sys.stderr)
    argv = sys.argv[1:]
    if argv and argv[0].endswith(".json") and not argv[0].startswith("-"):
        # `xqo hq.json [command ...]`: the file is the master config; no command = serve
        argv = ["--config", argv[0], *(argv[1:] or ["serve"])]
    a = p.parse_args(argv)
    _server_settings(a)

    if a.cmd in plugin_cmds:
        plugin_cmds[a.cmd].run_cli(a)
        return

    if a.cmd == "init":
        dst = Path(a.path)
        if dst.exists():
            sys.exit(f"{dst} already exists")
        cfg = json.loads(PACKAGED_CONFIG.read_text(encoding="utf-8"))
        dst.write_text(json.dumps({"server": INIT_SERVER, **cfg}, indent=2) + "\n", encoding="utf-8")
        ddir = dst.parent / "departments"
        if not ddir.exists():
            shutil.copytree(PACKAGED_DEPARTMENTS, ddir)
            print(f"wrote {ddir}/ ({', '.join(p.stem for p in sorted(ddir.glob('*.json')))})")
        print(f"wrote {dst}")
        return

    data = Path(a.data_dir)
    if a.cmd == "drain":
        _drain(a)
        return
    if a.cmd == "check":
        _check(a, data)
        return
    if a.cmd == "backup":
        _backup(a, lambda: build(a.config, data / "artifacts", str(data / "logs" / "events.jsonl"), mock=True,
                                 departments=a.departments, departments_dir=a.departments_dir,
                                 run_migrations=False), data)
        return
    if a.cmd in ("ask", "steer", "pause", "resume"):
        _talk(a)
        return
    if a.cmd in ("archive", "unarchive"):
        _archive(a, lambda: build(a.config, data / "artifacts", str(data / "logs" / "events.jsonl"), mock=True,
                                  departments=a.departments, departments_dir=a.departments_dir))
        return
    if a.cmd in ("cancel", "delete", "restore"):
        _manage(a, lambda: build(a.config, data / "artifacts", str(data / "logs" / "events.jsonl"), mock=True,
                                 departments=a.departments, departments_dir=a.departments_dir))
        return
    try:
        bus = build(a.config, data / "artifacts", str(data / "logs" / "events.jsonl"), mock=a.mock or None,
                    departments=a.departments, departments_dir=a.departments_dir)
    except ValueError as e:
        sys.exit(f"config error: {e}")
    names = ", ".join(f"{d.name} (head {d.head})" for d in bus.departments.values()) or "none"
    print(f"Departments: {names}")
    for m in bus.migrations_applied:
        print(f"Data migrated: {m}")
    for w in getattr(bus, "config_warnings", []):
        print(f"warning: {w}", file=sys.stderr)
    ops = _ops(a.config)
    bus.data_dir, bus.backup_dir = data, ops["dir"]
    if a.cmd == "serve":
        import uvicorn

        from .web import create_app

        bus.office_edit = a.edit or bool(getattr(getattr(bus, "office", None), "edit", False))  # --edit or "office": {"edit": true}
        print(f"Monitor: http://{a.host}:{a.port}" + ("  (office editing on)" if bus.office_edit else ""))
        side = []
        for p in bus.plugins.active():
            try:
                for line in p.banner():
                    print(line)
                side += p.side_servers()
            except Exception as e:
                print(f"warning: plugin {p.name}: {type(e).__name__}: {e}", file=sys.stderr)
        print("Plugins: " + ", ".join(f"{n} ({st.state})" for n, st in sorted(bus.plugins.states.items())
                                      if st.state in ("active", "failed")))
        servers = [uvicorn.Server(uvicorn.Config(create_app(bus), host=a.host, port=a.port, log_level="warning"))]
        servers += [uvicorn.Server(uvicorn.Config(app, host=h, port=pt, log_level="warning")) for app, h, pt in side]
        if len(servers) > 1:
            asyncio.run(_serve_all(servers))
        else:
            servers[0].run()
    else:
        from .attachments import validate
        try:
            files = validate([(Path(f).name, None, Path(f).read_bytes()) for f in a.attach])
        except (OSError, ValueError) as e:
            sys.exit(f"attachment: {e}")
        task = asyncio.run(_run_cli(bus, a.prompt, a.agent, files))
        print(f"\n{task.name}\n[{task.status}] {task.result}")
        print(f"\nArtifacts: {len(bus.artifacts.list(task.id))} saved under {bus.artifacts.root}")


if __name__ == "__main__":
    main()
