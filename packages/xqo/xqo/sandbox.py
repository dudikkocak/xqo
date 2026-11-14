"""Run agent-produced code against a copy of the task workspace: locally, or in Docker when the docker plugin is on.

Python projects run inside a per-task virtual environment, always created and filled with uv:
``artifacts/<task>/.venv`` on the local backend, ``artifacts/<task>/.venv-docker`` (mounted at /venv) with Docker,
where uv comes from the image. Dependencies from pyproject.toml and requirements*.txt are installed once and
reinstalled only when those files change. Without uv the code runs without a venv and the result says why.
Inside a task venv, `pip install …` (and `python -m pip …`) is run as `uv pip install …` on the task venv, the venv
gets `pip` shims that call uv, and xqo's own venv is dropped from PATH, so packages never land in it."""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import signal
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

MAX_OUTPUT = 12_000


@dataclass
class SandboxConfig:
    backend: str = "auto"            # auto | docker | local | off
    image: str = "astral/uv:python3.12-bookworm-slim"  # docker: needs uv and python on its PATH
    timeout: float = 120.0
    network: bool = False            # docker only
    memory: str = "1g"               # docker only
    cpus: str = "1"                  # docker only
    venv: bool = True                # per-task virtual environment for Python projects (local and docker)
    venv_tool: str = "uv"            # always uv; kept so older configs ("auto", "venv") still load
    python: str = ""                 # interpreter / version for the venv ("" = uv's choice)
    setup_timeout: float = 600.0     # seconds for creating the venv and installing dependencies
    setup_network: bool = True       # docker only: allow network while installing dependencies (runs stay offline)
    run_dir: str = ""                # where run copies go ("" = system temp dir; with Docker ~/.cache/xqo/runs)
    # local backend: Node/Go/Rust/.NET components get versions, shared download caches and per-task dependencies
    toolchains: bool = True
    versions: str = "auto"           # auto (mise if installed, else the host's) | mise | host
    cache_dir: str = ""              # shared download caches ("" = ~/.cache/xqo/toolchains; XQO_CACHE_DIR)
    node_modules: str = "auto"       # auto (link; a real copy for Next) | link | clone
    install_scripts: bool = True     # run npm/pnpm/bun install scripts (false: --ignore-scripts)
    components: list = field(default_factory=list)  # [{"path": "web", "toolchain": "node"}] instead of detection
    # docker: image per toolchain ("" = official image at the project's version), base of composite images
    images: dict = field(default_factory=dict)       # {"node": "node:20-bookworm", "go": ..., "rust": ..., "dotnet": ...}
    composite_base: str = ""                          # "" = buildpack-deps:bookworm
    ca_file: str = ""                # docker setup: CA bundle to trust ("" = the host's SSL_CERT_FILE & co., if any)
    network_requests: bool = True    # offline runs: agents may ask the user for network (request_network)

    @classmethod
    def from_dict(cls, d: dict | None) -> "SandboxConfig":
        d = dict(d or {})
        d["backend"] = os.getenv("XQO_SANDBOX", d.get("backend", "auto"))
        d["run_dir"] = os.getenv("XQO_RUN_DIR", d.get("run_dir", ""))
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def _cut(s: bytes) -> str:
    t = s.decode("utf-8", "replace")
    return t if len(t) <= MAX_OUTPUT else t[: MAX_OUTPUT // 2] + "\n…[truncated]…\n" + t[-MAX_OUTPUT // 2:]


PY_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile")
_env_locks: dict[str, asyncio.Lock] = {}


# Node/Go/Rust/.NET support is the toolchains plugin (xqo/plugins/toolchains/chains.py); it installs itself here.
toolchains = None
SKIP_DIRS = {".git", "node_modules", "vendor", "target", ".venv", "venv", "__pycache__", ".skills", ".pytest_cache"}
PY_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile")


def use_toolchains(module) -> None:
    """Called by the toolchains plugin's register(): detect / prepare / copy_ignore / describe come from `module`."""
    global toolchains
    toolchains = module


def walk(work: Path, pattern: str):
    """Files matching `pattern` below `work`, never inside dependency / VCS folders."""
    if toolchains:
        yield from toolchains.walk(work, pattern)
        return
    for root, dirs, files in os.walk(work):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        yield from (Path(root) / f for f in files if Path(f).match(pattern))


def is_python_project(work: Path) -> bool:
    """Python markers at the root, or .py files (outside node_modules & co.) and no other toolchain's marker."""
    if toolchains:
        return toolchains.is_python_project(work)
    return any((work / m).is_file() for m in PY_MARKERS) or any(walk(work, "*.py"))


def _pyproject_requirements(path: Path) -> list[str]:
    """Dependencies, optional dependencies and dependency groups from pyproject.toml (not the project itself)."""
    try:
        import tomllib
    except ImportError:  # Python < 3.11
        return []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    proj = data.get("project", {})
    reqs = list(proj.get("dependencies", []))
    for extra in (proj.get("optional-dependencies") or {}).values():
        reqs += extra
    for group in (data.get("dependency-groups") or {}).values():
        reqs += [r for r in group if isinstance(r, str)]
    return list(dict.fromkeys(reqs))


def _env_bin(env: Path) -> Path:
    return env / ("Scripts" if os.name == "nt" else "bin")


def _env_python(env: Path) -> Path:
    return _env_bin(env) / ("python.exe" if os.name == "nt" else "python")


_PIP = re.compile(r"^(\s*)(?:(?:python(?:3(?:\.\d+)?)?|py)(?:\.exe)?\s+-m\s+pip|pip(?:3(?:\.\d+)?)?(?:\.exe)?)\s+"
                  r"(install|uninstall|list|freeze|show|check)(?=\s|$)(.*)$", re.S | re.I)
_SEP = re.compile(r"(&&|\|\||[;&|\n])")


def uv_pip_command(command: str) -> str:
    """Rewrite `pip …` / `python -m pip …` steps of a shell command to `uv pip …`, so packages go into the task's venv
    (uv pip targets $VIRTUAL_ENV, which runs set to it) and never into whatever pip is first on PATH. No interpreter
    path goes into the command: cmd.exe and sh quote differently."""
    parts = _SEP.split(command)
    for i in range(0, len(parts), 2):  # odd items are the separators
        m = _PIP.match(parts[i])
        if m:
            lead, sub, rest = m.groups()
            rest = re.sub(r"\s+--user(?=\s|$)", "", rest)  # uv refuses --user; the venv is the target anyway
            parts[i] = f"{lead}uv pip {sub.lower()}{rest}"
    return "".join(parts)

# what uv needs from our environment to install packages: its settings, proxies, certificates, Windows profile dirs
_UV_PASS = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
            "LOCALAPPDATA", "APPDATA", "USERPROFILE", "TEMP", "TMP"}


def _write_pip_shims(env: Path) -> None:
    """uv venvs have no pip: add `pip`/`pip3` that run uv pip on this venv, so a pip found on PATH (by scripts too)
    is never another environment's."""
    b = _env_bin(env)
    for name in ("pip", "pip3"):
        if os.name == "nt":
            f, body = b / f"{name}.cmd", '@echo off\r\nuv pip %* --python "%~dp0python.exe"\r\n'
        else:
            f, body = b / name, '#!/bin/sh\nexec uv pip "$@" --python "$(dirname "$0")/python"\n'
        if b.is_dir() and not f.exists() and not f.with_suffix(".exe").exists():
            f.write_text(body)
            if os.name != "nt":
                f.chmod(0o755)


def _strip_outer_venvs(path: str) -> str:
    """PATH without the bin dir of the venv the xqo itself runs in (or that was active when it started)."""
    outer = [os.environ.get("VIRTUAL_ENV", ""), sys.prefix if sys.prefix != sys.base_prefix else ""]
    norm = lambda p: os.path.normcase(os.path.normpath(p))  # noqa: E731
    drop = {norm(str(_env_bin(Path(o)))) for o in outer if o}
    return os.pathsep.join(p for p in path.split(os.pathsep) if p and norm(p) not in drop)


def _uses_tests(work: Path) -> bool:
    return (work / "tests").is_dir() or any(walk(work, "test_*.py")) or any(walk(work, "*_test.py"))


def docker_available() -> bool:
    """True if the docker CLI exists and its daemon answers."""
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True,
                              timeout=8).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class Backend:
    """Where and how a run's command executes. A new backend (e.g. a remote runner) subclasses this, sets `name`
    and implements `command`; register it with register_backend() or name it in agents.json as
    "sandbox": {"backend": "package.module:ClassName"}. Sandbox.run copies the workspace, then calls `command` and
    runs what it returns with the timeout, cancel and output handling every backend shares."""
    name = ""
    toolchains = False  # True: the run copy gets Node/Go/Rust/.NET setup (toolchains plugin) before `command`
    isolates_network = False  # True: runs are offline unless `network=True` (agents can then request_network)

    def available(self) -> bool:
        return True

    async def command(self, sb: "Sandbox", work: Path, command: str, env_dir: Path | None,
                      comps: list, network: bool = False) -> dict:
        """{"argv": [...] (exec) or None (shell runs "run_cmd"), "env": dict | None, "run_cmd": str,
        "container": name to `docker kill` on timeout/cancel or None, "result": extra fields for the result}.
        `network`: the user approved network for this run (only passed to backends that isolate it)."""
        raise NotImplementedError


class LocalBackend(Backend):
    """A subprocess in the run copy with a minimal environment and a timeout. NOT a security boundary."""
    name = "local"
    toolchains = True

    async def command(self, sb, work, command, env_dir, comps):
        cfg, res, run_cmd = sb.config, {}, command
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(work), "PYTHONDONTWRITEBYTECODE": "1",
               "PYTHONPATH": str(work), "LANG": "C.UTF-8"}
        if sys.platform == "win32":
            env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
            env.update({k: os.environ[k] for k in ("PATHEXT", "COMSPEC") if k in os.environ})
        if toolchains and env_dir is not None and cfg.toolchains and comps:  # Node/Go/Rust/.NET: versions, caches, dependencies
            prep = await toolchains.prepare(toolchains.HostRunner(sb._sh, cfg), work, comps, env_dir.parent / ".deps",
                                            cfg, env)
            if prep.info:
                res["toolchains"] = prep.info
            if prep.env:
                env = prep.env
            if prep.managed and (note := toolchains.install_note(command)):
                res["notes"] = [note]
        venv = None
        if env_dir is not None and cfg.venv and is_python_project(work):
            venv = await sb.prepare_env(work, env_dir)
        if venv and not venv.get("error"):  # run inside the task's venv (not ours); pip and uv use it too
            env.update({"VIRTUAL_ENV": str(env_dir), "UV_PROJECT_ENVIRONMENT": str(env_dir),
                        "PATH": str(_env_bin(env_dir)) + os.pathsep + _strip_outer_venvs(env["PATH"])})
            run_cmd = uv_pip_command(command)
            env.update({k: v for k, v in os.environ.items() if k.upper() in _UV_PASS or (
                k.upper().startswith("UV_") and k.upper() not in ("UV_PROJECT_ENVIRONMENT", "UV_PYTHON"))})
            if os.environ.get("UV_CACHE_DIR") or os.environ.get("HOME"):
                env["UV_CACHE_DIR"] = os.environ.get("UV_CACHE_DIR") or str(Path.home() / ".cache" / "uv")
        if venv:
            res["venv"] = venv
        # argv None: the shell gets the command as one string (cmd.exe would mangle a list's quotes)
        return {"argv": None, "env": env, "run_cmd": run_cmd, "container": None, "result": res}



_build_locks: dict[str, asyncio.Lock] = {}
CA_IN_CONTAINER = "/etc/ssl/orch-ca.pem"


def _setup_net_args(cfg) -> list[str]:
    return _net_args(cfg) if cfg.setup_network else []


def _net_args(cfg) -> list[str]:
    """What a container needs to download through the host's network: its CA bundle (a corporate or proxy CA;
    mounted read-only) and its proxies, unless they point at the host's loopback, which a container can't reach."""
    args = []
    ca = cfg.ca_file or next((os.environ[k] for k in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS")
                              if os.environ.get(k) and Path(os.environ[k]).is_file()), "")
    if ca and Path(ca).expanduser().is_file():
        args += ["-v", f"{Path(ca).expanduser().resolve()}:{CA_IN_CONTAINER}:ro"]
        args += [x for k in ("SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS", "CARGO_HTTP_CAINFO", "GIT_SSL_CAINFO")
                 for x in ("-e", f"{k}={CA_IN_CONTAINER}")]
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"):
        v = os.environ.get(k, "")
        if v and not re.search(r"//(localhost|127\.|\[::1\])", v):
            args += ["-e", f"{k}={v}"]
    return args


BACKENDS: dict[str, type[Backend]] = {"local": LocalBackend}  # "docker" and others come from plugins (plugins/docker)


def register_backend(cls: type[Backend]) -> type[Backend]:
    """Make a Backend subclass selectable by its `name` (usable as a class decorator)."""
    BACKENDS[cls.name] = cls
    return cls


def backend_class(name: str) -> type[Backend]:
    if name in BACKENDS:
        return BACKENDS[name]
    if ":" in name:
        mod, _, attr = name.partition(":")
        try:
            cls = getattr(importlib.import_module(mod), attr)
        except (ImportError, AttributeError) as e:
            raise ValueError(f"sandbox backend {name!r} could not be loaded: {e}") from e
        if not (isinstance(cls, type) and issubclass(cls, Backend)):
            raise ValueError(f"sandbox backend {name!r} is not a sandbox.Backend subclass")
        return cls
    hint = " (pip install xqo-docker)" if name == "docker" else ""
    raise ValueError(f"unknown sandbox backend {name!r}{hint}: use auto, off, {', '.join(BACKENDS)} or 'module:Class'")


class Sandbox:
    def __init__(self, config: SandboxConfig | None = None):
        self.config = config or SandboxConfig()
        self._auto: str | None = None
        self._impls: dict[str, Backend] = {}
        if self.config.backend not in ("auto", "off"):
            backend_class(self.config.backend)  # an unknown backend stops startup with a clear message

    @property
    def backend(self) -> str:
        b = self.config.backend
        if b == "auto":
            if self._auto is None:
                docker = BACKENDS.get("docker")  # registered by the docker plugin when it is enabled
                self._auto = "docker" if docker and docker().available() else "local"
            return self._auto
        return b

    def run_root(self, backend: str) -> Path | None:
        """Parent dir of the per-run copies: `run_dir` if set, else for Docker a dir under the home folder, which
        Docker Desktop shares by default (it doesn't share /tmp), else None for the system temp dir."""
        if self.config.run_dir:
            root = Path(self.config.run_dir).expanduser()
        elif backend == "docker":
            root = Path.home() / ".cache" / "xqo" / "runs"
        else:
            return None
        root = root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        if str(root) not in _swept:  # once per process: drop copies left behind by a killed xqo
            _swept.add(str(root))
            _sweep_stale(root)
        return root

    def impl(self, backend: str | None = None) -> Backend:
        backend = backend or self.backend
        return self._impls.get(backend) or self._impls.setdefault(backend, backend_class(backend)())

    @property
    def network_requestable(self) -> bool:
        """Runs are offline and agents may ask the user for network (request_network)."""
        b = self.backend
        return (self.config.network_requests and not self.config.network and b != "off"
                and self.impl(b).isolates_network)

    @property
    def venv_tool(self) -> str | None:
        return "uv" if shutil.which("uv") else None  # venvs are always made with uv, never python -m venv / pip

    async def _sh(self, argv: list[str], cwd: Path, env: dict, timeout: float) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(*argv, cwd=str(cwd), env=env, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                                                    start_new_session=os.name != "nt")
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            _kill_tree(proc)
            out, _ = await proc.communicate()
            return -1, _cut(out) + "\n[timed out]"
        except asyncio.CancelledError:
            await _stop(proc)
            raise
        return proc.returncode, _cut(out)

    async def prepare_env(self, work: Path, env_dir: Path) -> dict:
        """Create (once) and update the task's virtual environment for the project in `work`."""
        tool = self.venv_tool
        if tool is None:
            return {"error": "uv is not installed (virtual environments are always created with uv)"}
        reqs_files, py_reqs, tests, digest = _deps(work, [tool, self.config.python])
        stamp = env_dir / ".orch-deps.json"
        info = {"path": str(env_dir), "tool": tool, "created": False, "installed": [], "log": ""}
        async with _env_locks.setdefault(str(env_dir), asyncio.Lock()):
            py = _env_python(env_dir)
            base_env = {**os.environ, "VIRTUAL_ENV": str(env_dir)}
            logs = []
            if not py.exists():
                env_dir.parent.mkdir(parents=True, exist_ok=True)
                argv = ["uv", "venv", "--quiet", str(env_dir)] + (["--python", self.config.python]
                                                                   if self.config.python else [])
                code, out = await self._sh(argv, work, base_env, self.config.setup_timeout)
                logs.append(f"$ {' '.join(argv)}\n{out}")
                if code != 0:
                    return {**info, "error": f"creating the venv failed (exit {code})", "log": "\n".join(logs)}
                info["created"] = True
            _write_pip_shims(env_dir)
            try:
                done = json.loads(stamp.read_text())["digest"] == digest
            except (OSError, ValueError, KeyError):
                done = False
            if not done:
                pip = ["uv", "pip", "install", "--quiet", "--python", str(py)]
                steps = [("pyproject.toml", py_reqs)] if py_reqs else []
                steps += [(f, ["-r", f]) for f in reqs_files]
                for label, args in steps:
                    code, out = await self._sh(pip + args, work, base_env, self.config.setup_timeout)
                    logs.append(f"$ {' '.join(pip + args)}\n{out}")
                    if code != 0:
                        return {**info, "error": f"installing {label} failed (exit {code})", "log": "\n".join(logs)}
                    info["installed"].append(label)
                if tests:  # the tester almost always needs pytest
                    code, _ = await self._sh([str(py), "-c", "import pytest"], work, base_env, 60)
                    if code != 0:
                        code, out = await self._sh(pip + ["pytest"], work, base_env, self.config.setup_timeout)
                        logs.append(f"$ {' '.join(pip + ['pytest'])}\n{out}")
                        if code == 0:
                            info["installed"].append("pytest")
                stamp.write_text(json.dumps({"digest": digest, "installed": info["installed"]}))
            else:
                info["reused"] = True
            info["log"] = "\n".join(logs)
            return info

    async def build_image(self, tag: str, dockerfile: str, scratch: Path) -> dict:
        """Build a composite toolchain image once (kept by Docker under `tag`)."""
        async with _build_locks.setdefault(tag, asyncio.Lock()):
            scratch.mkdir(parents=True, exist_ok=True)
            code, _ = await self._sh(["docker", "image", "inspect", tag], scratch, dict(os.environ), 60)
            if code == 0:
                return {"reused": True}
            ctx = scratch / "image-build"
            ctx.mkdir(parents=True, exist_ok=True)
            (ctx / "Dockerfile").write_text(dockerfile, encoding="utf-8")
            code, out = await self._sh(["docker", "build", "-q", "-t", tag, str(ctx)], ctx, dict(os.environ),
                                       self.config.setup_timeout)
            if code != 0:
                return {"error": f"building {tag} failed (exit {code})", "log": out}
            return {"built": True}

    async def prepare_docker_env(self, work: Path, env_dir: Path, image: str = "") -> dict:
        """Create (once) and update the task's venv for Docker runs with the image's uv: `env_dir` is mounted at
        /venv, the env itself is /venv/env (built against the image's Python) and uv's cache is /venv/cache."""
        image = image or self.config.image
        reqs_files, py_reqs, tests, digest = _deps(work, ["docker", image, self.config.python])
        stamp = env_dir / ".orch-deps.json"
        info = {"path": str(env_dir), "tool": "uv", "created": False, "installed": [], "log": ""}
        async with _env_locks.setdefault(str(env_dir), asyncio.Lock()):
            try:
                if json.loads(stamp.read_text())["digest"] == digest and (env_dir / "env").is_dir():
                    return {**info, "reused": True}
            except (OSError, ValueError, KeyError):
                pass
            env_dir.mkdir(parents=True, exist_ok=True)
            q = shlex.join
            pip = ["uv", "pip", "install", "--quiet", "--python", DOCKER_PY]
            steps = [("pyproject.toml", py_reqs)] if py_reqs else []
            steps += [(f, ["-r", f]) for f in reqs_files]
            lines = ["set -e"]
            if not (env_dir / "env").is_dir():
                info["created"] = True
                mk = ["uv", "venv", "--quiet", "/venv/env"] + (["--python", self.config.python] if self.config.python else [])
                lines.append(f"echo '$ {q(mk)}'; {q(mk)}")
            for _, args in steps:
                lines.append(f"echo {shlex.quote('$ ' + q(pip + args))}; {q(pip + args)}")
            if tests:  # the tester almost always needs pytest
                lines.append(f"{DOCKER_PY} -c 'import pytest' 2>/dev/null || {{ echo '$ {q(pip + ['pytest'])}'; {q(pip + ['pytest'])}; echo {PYTEST_MARK}; }}")
            name = f"orch-env-{uuid.uuid4().hex[:12]}"
            argv = ["docker", "run", "--rm", "-i", "--name", name, "--memory", self.config.memory,
                    "--cpus", self.config.cpus, "--pids-limit", "256", *_docker_user(), "-v", f"{work}:/work", "-v", f"{env_dir}:/venv",
                    "-w", "/work", "-e", "HOME=/tmp", "-e", "UV_CACHE_DIR=/venv/cache",
                    "-e", "UV_PYTHON_INSTALL_DIR=/venv/python", *_setup_net_args(self.config)]
            if not self.config.setup_network:
                argv += ["--network", "none"]
            argv += [image, "sh", "-c", "\n".join(lines)]
            try:
                code, out = await self._sh(argv, work, dict(os.environ), self.config.setup_timeout)
            except asyncio.CancelledError:
                await _docker_kill(name)
                raise
            if code == -1:  # timed out: the container may still be running
                await _docker_kill(name)
            info["log"] = out.replace(PYTEST_MARK + "\n", "")
            if code != 0:
                return {**info, "error": mount_hint(out) or f"setting up the venv in {image} failed (exit {code})"}
            info["installed"] = [label for label, _ in steps] + (["pytest"] if PYTEST_MARK in out else [])
            stamp.write_text(json.dumps({"digest": digest, "installed": info["installed"]}))
            return info

    async def run(self, workspace: Path, command: str, timeout: float | None = None,
                  env_dir: Path | None = None, extra: dict[str, Path] | None = None, network: bool = False) -> dict:
        """Copy the workspace to a temp dir, run `command` there, return exit code and output.

        `env_dir`: where this task's virtual environment lives (local backend, Python projects).
        `extra`: the agent's skill packs {name: folder}, copied to .skills/<name>/ in the run copy.
        `network`: the user approved network for this run (backends that isolate it; others always have it)."""
        backend = self.backend
        if backend == "off":
            raise RuntimeError("code execution is disabled (sandbox backend 'off')")
        limit = min(float(timeout or self.config.timeout), self.config.timeout)
        impl = self.impl(backend)
        network = bool(network and impl.isolates_network and not self.config.network)
        comps = (toolchains.detect(workspace, self.config.components, self.config.node_modules)
                 if toolchains and impl.toolchains and self.config.toolchains and env_dir is not None
                 and workspace.exists() else [])
        ignore = toolchains.copy_ignore(workspace, comps, RUN_COPY_SKIP) if toolchains else \
            (lambda d, names: {n for n in names if n in RUN_COPY_SKIP})
        with tempfile.TemporaryDirectory(prefix="orch-run-", dir=self.run_root(backend)) as tmp:
            work = Path(tmp) / "work"
            if workspace.exists():
                shutil.copytree(workspace, work, ignore=ignore)
            else:
                work.mkdir()
            for name, folder in (extra or {}).items():
                if folder.is_dir():
                    shutil.copytree(folder, work / ".skills" / name, ignore=shutil.ignore_patterns(".git"),
                                    dirs_exist_ok=True)
            spec = await impl.command(self, work, command, env_dir, comps, **({"network": True} if network else {}))
            argv, env, run_cmd, container = spec["argv"], spec["env"], spec["run_cmd"], spec.get("container")
            start = time.monotonic()
            kw = dict(cwd=str(work), env=env, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                      stderr=asyncio.subprocess.PIPE, start_new_session=os.name != "nt")
            proc = await (asyncio.create_subprocess_exec(*argv, **kw) if argv else
                          asyncio.create_subprocess_shell(run_cmd, **kw))  # /bin/sh -c, or %COMSPEC% /c on Windows
            timed_out = False
            try:
                out, err = await asyncio.wait_for(proc.communicate(), limit)
            except asyncio.TimeoutError:
                timed_out = True
                _kill_tree(proc)
                await _docker_kill(container)
                out, err = await proc.communicate()
            except asyncio.CancelledError:  # the task was cancelled: stop the run and its container
                await _stop(proc, container)
                raise
            res = {"backend": backend, "command": command, "exit_code": proc.returncode, "timed_out": timed_out,
                   "seconds": round(time.monotonic() - start, 2), "stdout": _cut(out), "stderr": _cut(err)}
            res.update(spec.get("result") or {})
            if network:
                res["network"] = True
            if backend == "docker" and proc.returncode == 125 and (hint := mount_hint(res["stderr"])):
                res["error"] = hint  # the container never started: say how to fix the host, not just Docker's text
            if run_cmd != command:
                res["ran"] = run_cmd
            return res


DOCKER_PY = "/venv/env/bin/python"
PYTEST_MARK = "@@orch: installed pytest"
DOCKER_PATH = "/usr/local/sbin:/usr/local/bin:/usr/local/go/bin:/usr/local/cargo/bin:/usr/share/dotnet:/usr/sbin:/usr/bin:/sbin:/bin"


def _deps(work: Path, key: list) -> tuple[list[str], list[str], bool, str]:
    """requirements files, pyproject dependencies, whether pytest is needed, and a digest of all that plus `key`."""
    reqs_files = sorted(str(p.relative_to(work)) for p in work.glob("requirements*.txt"))
    py_reqs = _pyproject_requirements(work / "pyproject.toml") if (work / "pyproject.toml").is_file() else []
    tests = _uses_tests(work)
    digest = hashlib.sha256(json.dumps(key + [py_reqs, tests] + [
        (f, (work / f).read_text(encoding="utf-8", errors="replace")) for f in reqs_files]).encode()).hexdigest()
    return reqs_files, py_reqs, tests, digest


_swept: set[str] = set()
STALE_RUN_SECONDS = 24 * 3600


# not copied into a run: git and caches the run rebuilds anyway (node_modules and dist ARE copied: runs are offline)
RUN_COPY_SKIP = (".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache")


def _sweep_stale(root: Path) -> None:
    """Remove orch-run-* dirs older than a day (runs in progress are minutes old at most)."""
    cutoff = time.time() - STALE_RUN_SECONDS
    for d in root.glob("orch-run-*"):
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, **{_RMTREE_HOOK: _force_remove})
        except OSError:
            pass


_RMTREE_HOOK = "onexc" if sys.version_info >= (3, 12) else "onerror"  # onerror is deprecated in 3.12


def _force_remove(func, path, _exc) -> None:
    """rmtree onerror: clear the read-only bit (Windows, npm/git files) and retry once; give up quietly."""
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        func(path)
    except OSError:
        pass


def mount_hint(out: str) -> str:
    """A fix-it message when Docker refused to bind-mount a host path ("mounts denied"), else ""."""
    if "mounts denied" not in out:
        return ""
    m = re.search(r"path (\S+) is not shared", out)
    path = m.group(1) if m else "a host folder"
    return (f"Docker can't mount {path}: it isn't shared with Docker. Share it (or a parent folder) in Docker Desktop "
            "-> Settings -> Resources -> File Sharing, or move it under a shared folder such as your home folder: "
            "run copies go to sandbox.run_dir in agents.json (or XQO_RUN_DIR), task venvs live next to the task's artifacts")


def _docker_user() -> list[str]:
    """Run containers as the host user, so files in the mounted work dir and venv stay removable."""
    return ["--user", f"{os.getuid()}:{os.getgid()}"] if hasattr(os, "getuid") else []


def _kill_tree(proc) -> None:
    """Kill a process and everything it started (it runs in its own session/process group)."""
    try:
        if os.name != "nt":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass


async def _docker_kill(container: str | None) -> None:
    if not container:
        return
    try:
        p = await asyncio.create_subprocess_exec("docker", "kill", container, stdout=asyncio.subprocess.DEVNULL,
                                                 stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(p.wait(), 15)
    except (OSError, asyncio.TimeoutError):
        pass


async def _stop(proc, container: str | None = None) -> None:
    _kill_tree(proc)
    await _docker_kill(container)
    try:
        await asyncio.wait_for(proc.wait(), 5)
    except (asyncio.TimeoutError, ProcessLookupError):
        pass


def describe_venv(v: dict) -> str:
    if v.get("error"):
        return f"[venv ERROR: {v['error']}; ran without it]\n{v.get('log', '')[-3000:]}"
    what = "created" if v.get("created") else "reused" if v.get("reused") else "updated"
    inst = f", installed {', '.join(v['installed'])}" if v.get("installed") else ""
    return f"[venv {what} with {v['tool']}: {v['path']}{inst}]"


def format_result(r: dict) -> str:
    head = (f"$ {r['command']}\n[backend={r['backend']} exit={r['exit_code']} {r['seconds']}s"
            + (" TIMED OUT" if r["timed_out"] else "") + "]")
    if r.get("ran"):
        head += f"\n[ran as: {r['ran']}]"
    for t in (r.get("toolchains") or []) if toolchains else []:
        head += "\n" + toolchains.describe(t)
    if r.get("venv"):
        head += "\n" + describe_venv(r["venv"])
    if r.get("network"):
        head += "\n[network: on for this run (approved by the user)]"
    for n in r.get("notes") or []:
        head += f"\n[note: {n}]"
    if r.get("error"):
        head += f"\n[sandbox ERROR: {r['error']}]"
    return f"{head}\n--- stdout ---\n{r['stdout']}\n--- stderr ---\n{r['stderr']}"


# ---- public names for container backends (exported by xqo.api; the underscored names stay for core callers) ----
docker_kill = _docker_kill
docker_user = _docker_user
net_args = _net_args
setup_net_args = _setup_net_args


def current_toolchains():
    """The toolchains module installed with use_toolchains(), or None."""
    return toolchains
