"""xqo-flows-runner: `xqo worker`, a headless remote worker for xqo flows (see worker.py).

On the worker machine, next to its own agents.json:

    "flows_worker": {"hq": "http://hq.example:8200", "name": "gpu-box", "token_env": "XQO_WORKER_TOKEN", "jobs": 1}

then `xqo --config worker.json worker` (flags override the file: --hq, --name, --token-env, --jobs, --once).
The plugin does nothing inside `xqo serve`; it only adds the command."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

from xqo.api import Plugin, build

from .worker import HQClient, HQError, Worker

__all__ = ["RunnerPlugin", "Worker", "HQClient", "HQError", "settings"]

KEYS = {"hq", "name", "token_env", "jobs", "poll_wait"}


def settings(cfg: dict, a=None) -> dict:
    """The worker's settings: command-line flag, then environment, then "flows_worker" in the config file."""
    fw = cfg.get("flows_worker") or {}
    if not isinstance(fw, dict):
        raise ValueError("\"flows_worker\" must be an object")
    if fw.get("token"):
        raise ValueError("flows_worker has a plaintext token; put it in an environment variable and name that "
                         "with token_env")
    bad = set(fw) - KEYS
    if bad:
        raise ValueError(f"flows_worker: unknown key(s) {', '.join(sorted(bad))}; known: {', '.join(sorted(KEYS))}")

    def pick(flag, env, key, default=None):
        v = getattr(a, flag, None) if a is not None else None
        if v not in (None, ""):
            return v
        if os.getenv(env):
            return os.environ[env]
        return fw.get(key, default)

    s = {"hq": pick("hq", "XQO_WORKER_HQ", "hq"), "name": pick("name", "XQO_WORKER_NAME", "name"),
         "token_env": pick("token_env", "XQO_WORKER_TOKEN_ENV", "token_env", "XQO_WORKER_TOKEN"),
         "jobs": int(pick("jobs", "XQO_WORKER_JOBS", "jobs", 1)),
         "poll_wait": float(fw.get("poll_wait", 20))}
    if not s["hq"] or not s["name"]:
        raise ValueError("the worker needs the HQ's worker API address and its own name: --hq URL --name NAME "
                         "(or \"flows_worker\": {\"hq\": ..., \"name\": ...} in its agents.json)")
    s["token"] = os.getenv(s["token_env"], "")
    if not s["token"]:
        raise ValueError(f"set {s['token_env']} to this worker's token (the HQ lists the same token in "
                         f"flows.workers.allow.{s['name']})")
    return s


def _main(a) -> None:
    try:
        cfg = json.loads(Path(a.config).read_text(encoding="utf-8")) if Path(a.config).is_file() else {}
        s = settings(cfg, a)
    except (OSError, ValueError) as e:
        sys.exit(f"config error: {e}")
    data = Path(a.data_dir)
    try:
        bus = build(a.config, data / "artifacts", str(data / "logs" / "events.jsonl"), mock=a.mock or None,
                    departments=a.departments, departments_dir=a.departments_dir)
    except ValueError as e:
        sys.exit(f"config error: {e}")
    hq = HQClient(s["hq"], s["name"], s["token"])
    worker = Worker(bus, hq, s["jobs"], s["poll_wait"])

    async def go():
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, worker.stop)
            except (NotImplementedError, RuntimeError):  # Windows
                pass
        try:
            info = await worker.ping()
        except HQError as e:
            sys.exit(f"the HQ at {s['hq']} refused worker {s['name']}: {e}")
        except OSError as e:
            sys.exit(f"cannot reach the HQ at {s['hq']}: {e}")
        print(f"xqo worker {s['name']}: connected to HQ {info['hq']!r} at {s['hq']}, labels: "
              f"{', '.join(info['labels']) or '-'}, agents: {', '.join(bus.agents)}, jobs at a time: {s['jobs']}",
              flush=True)
        try:
            await worker.run(max_jobs=1 if a.once else None)
        except HQError as e:
            sys.exit(str(e))

    asyncio.run(go())


class RunnerPlugin(Plugin):
    name = "flows_runner"
    description = "Headless remote worker for xqo flows (the `xqo worker` command)"
    config_keys = ("flows_worker",)

    @classmethod
    def add_cli(cls, sub, url) -> list[str]:
        w = sub.add_parser("worker", help="run as a headless flows worker for an HQ (xqo-flows-runner)")
        w.add_argument("--hq", help="the HQ's worker API, e.g. http://hq.example:8200 (XQO_WORKER_HQ)")
        w.add_argument("--name", help="this worker's name as listed on the HQ (XQO_WORKER_NAME)")
        w.add_argument("--token-env", help="environment variable with the token (default XQO_WORKER_TOKEN)")
        w.add_argument("--jobs", type=int, help="jobs at a time (default 1)")
        w.add_argument("--once", action="store_true", help="run one job, then exit")
        return ["worker"]

    @classmethod
    def run_cli(cls, a) -> None:
        _main(a)

    def configure(self, ctx, cfg):
        return None  # nothing to do inside `xqo serve`: the worker is its own command
