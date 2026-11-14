"""Language toolchains for the local sandbox backend: Node (incl. Nuxt/Next), Go, Rust and .NET, next to Python's uv venv.

A workspace is split into *components*: folders with a marker file (package.json, go.mod, Cargo.toml, *.sln/*.csproj),
so a Go service with a Nuxt app in web/ is two components. Each run, before the command:

* versions: wanted versions come from .nvmrc/.node-version/package.json engines, go.mod (`toolchain`/`go`),
  global.json and mise's own files (.tool-versions, mise.toml). With `mise` on PATH they are installed once (shared,
  in mise's data dir) and put first on PATH; without it the host's toolchains are used and a mismatch is reported.
  Rust follows rust-toolchain(.toml) through rustup by itself.
* setup: dependencies are installed when a component's manifest or lockfile changed (digest stamp), else reused:
  npm ci / pnpm / yarn / bun into ``artifacts/<task>/.deps/node@<path>/`` (linked into the run copy as node_modules,
  never copied), ``go mod download``, ``cargo fetch``, ``dotnet restore``.
* caches: download caches (npm, pnpm store, yarn, Go modules, cargo registry, NuGet) are shared by all tasks under
  `cache_dir`; build outputs (node_modules, GOCACHE, cargo target, dotnet CLI home) belong to the task.

The local backend is NOT a security boundary: commands run as xqo's user, with network."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

KINDS = ("node", "go", "rust", "dotnet", "python")
# never searched for components, never linked (dependency, build and VCS folders)
SKIP_DIRS = {".git", "node_modules", "vendor", "target", "bin", "obj", ".venv", "venv", "dist", "build", ".next",
             ".nuxt", ".output", "__pycache__", ".skills", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".yarn",
             ".svelte-kit", ".turbo", ".cache", "coverage"}
MAX_DEPTH = 3
PY_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile")
NODE_LOCKS = ("package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb", "bun.lock")
NODE_EXTRA = (".npmrc", ".yarnrc", ".yarnrc.yml", ".pnpmfile.cjs", "pnpm-workspace.yaml")
DOTNET_PROJ = (".sln", ".slnx", ".csproj", ".fsproj", ".vbproj")
DOTNET_FILES = {"global.json", "nuget.config", "directory.build.props", "directory.build.targets",
                "directory.packages.props", "packages.lock.json"}
# build outputs a run makes again: never copied into a run (only next to their project's marker file)
BUILD_DIRS = {"node": (".next", ".nuxt", ".output", ".svelte-kit", ".turbo"), "rust": ("target",),
              "dotnet": ("bin", "obj")}
# what toolchains need from our environment: proxies, certificates, registries, rustup/mise homes
PASS = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS", "GOPROXY", "GOPRIVATE", "GONOPROXY", "GONOSUMDB", "GOSUMDB", "GOINSECURE",
        "NPM_CONFIG_REGISTRY", "CARGO_HTTP_CAINFO", "CARGO_NET_GIT_FETCH_WITH_CLI", "RUSTUP_HOME", "RUSTUP_TOOLCHAIN",
        "MISE_DATA_DIR", "MISE_CONFIG_DIR", "MISE_CACHE_DIR", "LOCALAPPDATA", "APPDATA", "USERPROFILE", "TEMP", "TMP",
        "PROGRAMFILES", "PROGRAMDATA", "DOTNET_ROOT"}


@dataclass
class Component:
    path: str                     # posix path relative to the workspace, "." for the root
    kind: str                     # node | go | rust | dotnet | python
    mode: str = ""                # node: link | clone | inplace

    @property
    def slug(self) -> str:
        return f"{self.kind}@{'root' if self.path == '.' else re.sub(r'[^A-Za-z0-9._-]', '_', self.path)}"


@dataclass
class Prepared:
    env: dict = field(default_factory=dict)       # variables for the run (and setup)
    path: list = field(default_factory=list)      # dirs to put first on PATH
    info: list = field(default_factory=list)      # one dict per component (and one for versions)
    managed: list = field(default_factory=list)   # components whose deps the sandbox installs


# ---- detection -------------------------------------------------------------------------------------------------------
def _read_json(p: Path) -> dict:
    try:
        d = json.loads(p.read_text(encoding="utf-8-sig"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _kinds_at(d: Path, names: set[str]) -> list[str]:
    kinds = []
    if "package.json" in names:
        kinds.append("node")
    if "go.mod" in names:
        kinds.append("go")
    if "Cargo.toml" in names:
        kinds.append("rust")
    if any(n.lower().endswith(DOTNET_PROJ) for n in names):
        kinds.append("dotnet")
    if any(n in names for n in PY_MARKERS) or any(n.startswith("requirements") and n.endswith(".txt") for n in names):
        kinds.append("python")
    return kinds


def _workspace_root(d: Path, kind: str) -> bool:
    """A root that owns the components of the same kind below it (npm/pnpm/yarn workspaces, go.work, cargo
    [workspace], a .sln)."""
    if kind == "node":
        return bool(_read_json(d / "package.json").get("workspaces")) or (d / "pnpm-workspace.yaml").is_file()
    if kind == "go":
        return (d / "go.work").is_file()
    if kind == "rust":
        try:
            return re.search(r"(?m)^\s*\[workspace\]", (d / "Cargo.toml").read_text(encoding="utf-8")) is not None
        except OSError:
            return False
    if kind == "dotnet":
        return any(p.suffix.lower() in (".sln", ".slnx") for p in d.iterdir())
    return False


def detect(work: Path, overrides: list | None = None, node_modules: str = "auto") -> list[Component]:
    """Components of the workspace, breadth first, at most MAX_DEPTH folders deep. `overrides` ([{path, toolchain}])
    replaces detection."""
    if overrides:
        comps = [Component(str(o.get("path") or ".").strip("/") or ".", str(o.get("toolchain") or o.get("kind")))
                 for o in overrides if isinstance(o, dict) and (o.get("toolchain") or o.get("kind")) in KINDS]
    else:
        comps, owners = [], []  # owners: (path, kind) of workspace roots
        queue = [(work, 0)]
        while queue:
            d, depth = queue.pop(0)
            try:
                entries = sorted(d.iterdir(), key=lambda p: p.name)
            except OSError:
                continue
            names = {p.name for p in entries if p.is_file()}
            rel = "." if d == work else d.relative_to(work).as_posix()
            for kind in _kinds_at(d, names):
                if any(o[1] == kind and _under(rel, o[0]) for o in owners):
                    continue  # part of a workspace root found higher up
                if kind == "python" and rel != ".":
                    continue  # the Python venv is per workspace (uv), from the root's files
                comps.append(Component(rel, kind))
                if _workspace_root(d, kind):
                    owners.append((rel, kind))
            if depth < MAX_DEPTH:
                queue += [(p, depth + 1) for p in entries if p.is_dir() and p.name not in SKIP_DIRS
                          and not p.is_symlink() and not p.name.startswith(".")]
    for c in comps:
        if c.kind == "node":
            c.mode = _node_mode(work / c.path, node_modules)
    return comps


def _under(rel: str, root: str) -> bool:
    return root == "." or rel == root or rel.startswith(root + "/")


def _node_mode(d: Path, pref: str) -> str:
    pkg = _read_json(d / "package.json")
    deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {}),
            **(pkg.get("optionalDependencies") or {})}
    local = any(isinstance(v, str) and v.startswith(("file:", "link:", "workspace:", "portal:")) for v in deps.values())
    if pkg.get("workspaces") or (d / "pnpm-workspace.yaml").is_file() or local:
        return "inplace"  # the install needs the sources too: done in the run copy
    if pref in ("link", "clone"):
        return pref
    # Turbopack (Next) refuses a node_modules that links outside the project: give Next a real folder
    return "clone" if "next" in deps else "link"


def is_python_project(work: Path) -> bool:
    """Python markers at the root, or .py files in a workspace with no other toolchain's marker."""
    try:
        names = {p.name for p in work.iterdir() if p.is_file()}
    except OSError:
        return False
    kinds = _kinds_at(work, names)
    if "python" in kinds:
        return True
    return not kinds and any(walk(work, "*.py"))


def walk(work: Path, pattern: str):
    """Files matching `pattern` below `work`, never inside SKIP_DIRS (node_modules ships .py files: node-gyp)."""
    for root, dirs, files in os.walk(work):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if Path(f).match(pattern):
                yield Path(root) / f


def copy_ignore(src: Path, comps: list[Component], base: tuple):
    """shutil.copytree ignore: `base` names everywhere, plus build outputs next to their marker file and the
    node_modules of components whose deps the sandbox links in."""
    managed = {os.path.normcase(str((src / c.path).resolve())) for c in comps if c.kind == "node"
               and c.mode in ("link", "clone")}
    kinds_with_builds = {c.kind for c in comps}

    def ignore(d: str, names: list[str]) -> set[str]:
        out = {n for n in names if n in base}
        here = set(names)
        if "package.json" in here and "node" in kinds_with_builds:
            out |= here & set(BUILD_DIRS["node"])
            if "node_modules" in here and os.path.normcase(str(Path(d).resolve())) in managed:
                out.add("node_modules")
        if "Cargo.toml" in here and "rust" in kinds_with_builds:
            out |= here & set(BUILD_DIRS["rust"])
        if "dotnet" in kinds_with_builds and any(n.lower().endswith(DOTNET_PROJ) for n in here):
            out |= here & set(BUILD_DIRS["dotnet"])
        return out
    return ignore


# ---- versions --------------------------------------------------------------------------------------------------------
_PLAIN = re.compile(r"^\s*[v=]?(\d+(?:\.\d+){0,2})(?:\.x)?\s*$")


def wanted_versions(work: Path, comps: list[Component]) -> dict[str, str]:
    """{tool: version} the project asks for (mise tool names: node, go, dotnet)."""
    want: dict[str, str] = {}
    for c in comps:
        d = work / c.path
        v = ""
        if c.kind == "node":
            for f in (".nvmrc", ".node-version"):
                if (d / f).is_file() or (work / f).is_file():
                    raw = ((d / f) if (d / f).is_file() else (work / f)).read_text(encoding="utf-8").strip()
                    v = "lts" if raw.lower().startswith("lts") else raw.lstrip("v")
                    break
            else:
                eng = (_read_json(d / "package.json").get("engines") or {}).get("node", "")
                m = _PLAIN.match(eng) if isinstance(eng, str) else None
                v = m.group(1) if m else ""
        elif c.kind == "go":
            try:
                text = (d / "go.mod").read_text(encoding="utf-8")
            except OSError:
                text = ""
            m = re.search(r"(?m)^toolchain\s+go(\S+)", text) or re.search(r"(?m)^go\s+(\d+\.\d+(?:\.\d+)?)", text)
            v = m.group(1) if m else ""
        elif c.kind == "dotnet":
            g = d / "global.json" if (d / "global.json").is_file() else work / "global.json"
            v = str((_read_json(g).get("sdk") or {}).get("version") or "")
        if v and c.kind in ("node", "go", "dotnet"):
            want.setdefault(c.kind, v)
    return want


def mise_files(work: Path) -> list[Path]:
    return [work / f for f in (".tool-versions", "mise.toml", ".mise.toml") if (work / f).is_file()]


def host_version(tool: str, env: dict) -> str:
    exe = shutil.which({"node": "node", "go": "go", "dotnet": "dotnet"}[tool], path=env.get("PATH"))
    if not exe:
        return ""
    import subprocess
    args = {"node": ["--version"], "go": ["version"], "dotnet": ["--version"]}[tool]
    try:
        out = subprocess.run([exe, *args], capture_output=True, text=True, timeout=20, env=env).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""
    m = re.search(r"(\d+\.\d+(?:\.\d+)?)", out)
    return m.group(1) if m else ""


def version_matches(want: str, have: str, tool: str) -> bool:
    if not want or want == "lts":
        return True
    w, h = want.split("."), have.split(".")
    n = 1 if tool == "node" else min(len(w), 2 if tool == "go" else 3)  # node: major; go: minor; dotnet: as given
    return w[:n] == h[:n]


# ---- environment -----------------------------------------------------------------------------------------------------
def cache_root(cfg_dir: str) -> Path:
    d = os.getenv("XQO_CACHE_DIR") or cfg_dir or str(Path.home() / ".cache" / "xqo" / "toolchains")
    return Path(d).expanduser().resolve()


def base_env(cache, deps, comps: list[Component], host: bool = True) -> dict:
    """Variables that point every tool's caches at the shared cache dir or the task's deps dir (as the tools see
    them: host paths, or /cache and /deps in a container). `host`: also pass our proxy/certificate/rustup settings."""
    kinds = {c.kind for c in comps}
    env = {k: v for k, v in os.environ.items() if k.upper() in PASS} if host else {}
    if host and "RUSTUP_HOME" not in env and (Path.home() / ".rustup").is_dir():
        env["RUSTUP_HOME"] = str(Path.home() / ".rustup")  # HOME is the run copy: rustup must still find toolchains
    if "node" in kinds:
        env.update({"npm_config_cache": str(cache / "npm"), "npm_config_store_dir": str(cache / "pnpm-store"),
                    "npm_config_update_notifier": "false", "npm_config_fund": "false", "npm_config_audit": "false",
                    "YARN_CACHE_FOLDER": str(cache / "yarn"), "YARN_ENABLE_GLOBAL_CACHE": "false",
                    "YARN_NODE_LINKER": "node-modules", "YARN_ENABLE_TELEMETRY": "0",
                    "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0", "COREPACK_HOME": str(cache / "corepack"),
                    "BUN_INSTALL_CACHE_DIR": str(cache / "bun"),
                    "NEXT_TELEMETRY_DISABLED": "1", "NUXT_TELEMETRY_DISABLED": "1"})
    if "go" in kinds:
        env.update({"GOMODCACHE": str(cache / "gomod"), "GOCACHE": str(deps / "go" / "build"),
                    "GOPATH": str(deps / "go" / "path"), "GOFLAGS": "-modcacherw", "GOTELEMETRY": "off"})
    if "rust" in kinds:
        env.update({"CARGO_HOME": str(cache / "cargo"), "CARGO_TARGET_DIR": str(deps / "rust-target")})
    if "dotnet" in kinds:
        env.update({"NUGET_PACKAGES": str(cache / "nuget"), "NUGET_HTTP_CACHE_PATH": str(cache / "nuget-http"),
                    "DOTNET_CLI_HOME": str(deps / "dotnet"), "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1",
                    "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1", "DOTNET_GENERATE_ASPNET_CERTIFICATE": "false",
                    # no build servers left running: they hold the run's output pipes open until the timeout
                    "MSBUILDDISABLENODEREUSE": "1", "DOTNET_CLI_USE_MSBUILD_SERVER": "0", "UseSharedCompilation": "false"})
    return env


def digest(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def manifest_files(d: Path, kind: str) -> list[Path]:
    """Files whose content decides a component's dependencies."""
    if kind == "node":
        return [d / n for n in ("package.json", *NODE_LOCKS, *NODE_EXTRA) if (d / n).is_file()]
    if kind == "go":
        return [d / n for n in ("go.mod", "go.sum", "go.work", "go.work.sum") if (d / n).is_file()]
    if kind == "rust":
        return sorted(p for p in walk(d, "Cargo.toml")) + [d / n for n in ("Cargo.lock", "rust-toolchain",
                                                                              "rust-toolchain.toml") if (d / n).is_file()]
    if kind == "dotnet":
        return sorted(p for p in walk(d, "*") if p.suffix.lower() in DOTNET_PROJ or p.name.lower() in DOTNET_FILES)
    return []


def read_all(files: list[Path]) -> list:
    out = []
    for f in files:
        try:
            out.append((f.name, hashlib.sha256(f.read_bytes()).hexdigest()))
        except OSError:
            pass
    return out


def node_pm(d: Path) -> str:
    pm = str(_read_json(d / "package.json").get("packageManager") or "").split("@")[0]
    if pm in ("npm", "pnpm", "yarn", "bun"):
        return pm
    for lock, name in (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("bun.lockb", "bun"), ("bun.lock", "bun")):
        if (d / lock).is_file():
            return name
    return "npm"


def node_install_argv(d: Path, pm: str, scripts: bool) -> list[str]:
    if pm == "npm":
        locked = (d / "package-lock.json").is_file() or (d / "npm-shrinkwrap.json").is_file()
        argv = ["npm", "ci" if locked else "install", "--no-audit", "--no-fund"]
    elif pm == "pnpm":
        argv = ["pnpm", "install"] + (["--frozen-lockfile"] if (d / "pnpm-lock.yaml").is_file() else [])
    elif pm == "yarn":
        argv = ["yarn", "install"]
    else:
        argv = ["bun", "install"]
    if not scripts and pm != "yarn":  # yarn berry has no --ignore-scripts
        argv.append("--ignore-scripts")
    return argv


def resolve_exe(argv: list[str], env: dict) -> list[str] | None:
    """argv with its program resolved on the run's PATH (Windows: npm.cmd); pnpm/yarn through corepack if needed."""
    exe = shutil.which(argv[0], path=env.get("PATH"))
    if exe:
        return [exe, *argv[1:]]
    if argv[0] in ("pnpm", "yarn") and (cp := shutil.which("corepack", path=env.get("PATH"))):
        return [cp, *argv]
    return None


def dotnet_target(d: Path) -> list[str]:
    """`dotnet restore` needs one project or solution when a folder has several."""
    files = sorted(p.name for p in d.iterdir() if p.is_file() and p.suffix.lower() in DOTNET_PROJ)
    sln = [f for f in files if f.lower().endswith((".sln", ".slnx"))]
    return [sln[0]] if sln else files[:1]


def link_dir(target: Path, link: Path) -> None:
    """A directory symlink (a junction on Windows, which needs no privilege)."""
    if os.name == "nt":
        try:
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
            return
        except (ImportError, OSError):
            pass
    os.symlink(target, link, target_is_directory=True)


def clone_tree(src: Path, dst: Path) -> None:
    """Hard-link a tree (fast, no extra space), copying where links fail (another drive). Symlinks stay links."""
    can_link = [True]

    def cp(s, d):
        if can_link[0]:
            try:
                return os.link(s, d)
            except OSError:
                can_link[0] = False  # another drive / filesystem: copy the rest without trying again
        shutil.copy2(s, d)
    shutil.copytree(src, dst, symlinks=True, copy_function=cp)


_INSTALL = re.compile(r"(?:^|[;&|]\s*)(?:(?:npm|pnpm|bun)\s+(?:install|i|add)\s+(?!-)\S|yarn\s+add\s|go\s+get\s|"
                      r"cargo\s+add\s|dotnet\s+add\s.*\bpackage\s)")


def install_note(command: str) -> str:
    if _INSTALL.search(command):
        return ("this run works on a copy that is thrown away: packages added here are not in the workspace's "
                "package.json / go.mod / Cargo.toml / project file. Add them to that file with your file tools; the "
                "sandbox installs them before the next run.")
    return ""


def describe(info: dict) -> str:
    if info.get("kind") == "image":
        what = ("built" if info.get("built") else "") + (f"; ERROR: {info['error']}" if info.get("error") else "")
        tools = ", ".join(f"{k} {v}" for k, v in info.get("tools", {}).items()) if info.get("built") or info.get(
            "reused") or info.get("error") else ""
        return (f"[image {info['image']}" + (f" ({tools})" if tools else "") + (f": {what}" if what else "") + "]"
                + (f"\n{info.get('log', '')[-3000:]}" if info.get("error") else ""))
    if info.get("kind") == "versions":
        parts = [f"{t} {v}" for t, v in info.get("tools", {}).items()]
        head = f"[versions via {info['manager']}: {', '.join(parts) or 'from mise files'}"
        return head + (f"; ERROR: {info['error']}]" if info.get("error") else "]") + "".join(
            f"\n[warning: {w}]" for w in info.get("warnings", []))
    where = f"{info['kind']} {info['path']}"
    if info.get("error"):
        return f"[{where}: setup ERROR: {info['error']}]\n{info.get('log', '')[-3000:]}"
    what = info.get("setup") or ("reused" if info.get("reused") else "ready")
    return f"[{where}: {what}]" + "".join(f"\n[warning: {w}]" for w in info.get("warnings", []))

# ---- runners ---------------------------------------------------------------------------------------------------------
class HostRunner:
    """Setup commands on this machine (local backend): host paths, host toolchains, mise for versions."""
    versions = True   # wanted versions come from mise / the host (a container gets them from its image)
    host_env = True

    def __init__(self, sh, cfg):
        self._sh = sh
        self.cache = cache_root(cfg.cache_dir)

    def deps_view(self, deps: Path):
        return deps

    def exe(self, argv: list[str], env: dict) -> list[str] | None:
        return resolve_exe(argv, env)

    async def sh(self, argv, cwd: Path, env: dict, timeout: float):
        return await self._sh(argv, cwd, env, timeout)

    def link(self, target: Path, link: Path) -> None:
        link_dir(target, link)


# ---- setup -----------------------------------------------------------------------------------------------------------
_locks: dict = {}
_host_versions: dict = {}


def _lock(key: str):
    import asyncio
    return _locks.setdefault(key, asyncio.Lock())


def _stamp_ok(sdir: Path, key: str) -> bool:
    try:
        return json.loads((sdir / "stamp.json").read_text(encoding="utf-8"))["key"] == key
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _stamp(sdir: Path, key: str, **extra) -> None:
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "stamp.json").write_text(json.dumps({"key": key, **extra}), encoding="utf-8")


def _json_in(text: str) -> dict:
    """The JSON object in a command's output (stderr warnings are mixed in)."""
    a, b = text.find("{"), text.rfind("}")
    try:
        d = json.loads(text[a:b + 1]) if a >= 0 and b > a else {}
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


async def prepare(runner, work: Path, comps: list[Component], deps: Path, cfg, run_env: dict) -> Prepared:
    """Versions, caches and dependencies for every non-Python component of the run copy `work`.
    `runner`: HostRunner, or the Docker backend's runner (setup containers); `deps`: the task's deps dir on the
    host; `run_env`: the run's environment so far."""
    out = Prepared()
    tcomps = [c for c in comps if c.kind != "python"]
    if not tcomps and not (runner.versions and mise_files(work)):
        return out
    deps.mkdir(parents=True, exist_ok=True)
    runner.cache.mkdir(parents=True, exist_ok=True)
    cache_view = runner.cache if runner.host_env else PurePosixPath("/cache")
    env = {**run_env, **base_env(cache_view, runner.deps_view(deps), tcomps, host=runner.host_env)}
    vinfo = await _versions(runner.sh, work, wanted_versions(work, tcomps), deps, cfg, env) if runner.versions else {}
    if vinfo:
        out.info.append(vinfo)
        if vinfo.get("path"):
            env["PATH"] = os.pathsep.join([*vinfo["path"], env.get("PATH", "")])
        env.update(vinfo.get("env") or {})
        if vinfo.get("manager") == "mise" and "go" in (vinfo.get("tools") or {}):
            env["GOTOOLCHAIN"] = "local"  # mise installed the wanted Go: don't let go fetch another one
    for c in tcomps:
        out.info.append(await _setup(runner, work, c, deps, cfg, env, vinfo))
        out.managed.append(c)
    out.env = env
    return out


async def _versions(sh, work: Path, want: dict, deps: Path, cfg, env: dict) -> dict:
    files = mise_files(work)
    if not want and not files:
        return {}
    mode = (cfg.versions or "auto").lower()
    mise = shutil.which("mise") if mode in ("auto", "mise") else None
    info = {"kind": "versions", "manager": "mise" if mise else "host", "tools": dict(want)}
    if mise:
        specs = [f"{t}@{v}" for t, v in want.items()]
        key = digest(specs, read_all(files), mise)
        sdir = deps / "versions"
        async with _lock(str(sdir)):
            if _stamp_ok(sdir, key):
                st = json.loads((sdir / "stamp.json").read_text(encoding="utf-8"))
                return {**info, "reused": True, "path": st.get("path", []), "env": st.get("env", {})}
            # mise runs with our own environment (real HOME, its data dir); the run copy's mise files are trusted
            menv = {**os.environ, "MISE_TRUSTED_CONFIG_PATHS": str(work), "MISE_YES": "1"}
            logs = []
            for argv in ([mise, "install"] if files else None, [mise, "install", *specs] if specs else None):
                if argv:
                    code, o = await sh(argv, work, menv, cfg.setup_timeout)
                    logs.append(f"$ {' '.join(argv)}\n{o}")
                    if code != 0:
                        return {**info, "error": f"mise install failed (exit {code})", "log": "\n".join(logs)}
            code, o = await sh([mise, "env", "--json", *specs], work, menv, 120)
            data = _json_in(o) if code == 0 else {}
            if not data:
                return {**info, "error": f"mise env failed (exit {code})", "log": "\n".join(logs + [o])}
            have = set(env.get("PATH", "").split(os.pathsep)) | set(os.environ.get("PATH", "").split(os.pathsep))
            path = [p for p in str(data.get("PATH", "")).split(os.pathsep) if p and p not in have]
            extra = {k: v for k, v in data.items() if k != "PATH" and isinstance(v, str) and not k.startswith("MISE_")}
            _stamp(sdir, key, path=path, env=extra)
            return {**info, "installed": True, "path": path, "env": extra, "log": "\n".join(logs)}
    warnings = []
    if mode == "mise":
        warnings.append("mise is not installed: using the host's toolchains")
    if files and not mise:
        warnings.append(f"{', '.join(f.name for f in files)} found but mise is not installed: versions not applied")
    for tool, v in want.items():
        if tool == "go":
            continue  # go fetches the toolchain go.mod asks for by itself (GOTOOLCHAIN=auto, Go 1.21+)
        k = (tool, env.get("PATH"))
        if k not in _host_versions:
            _host_versions[k] = host_version(tool, env)
        have = _host_versions[k]
        if not have:
            warnings.append(f"the project wants {tool} {v}, but {tool} is not installed")
        elif not version_matches(v, have, tool):
            warnings.append(f"the project wants {tool} {v}, the host has {have} (install mise to get {v})")
    return {**info, "warnings": warnings} if warnings else {}  # host toolchains that fit: nothing to say


async def _setup(runner, work: Path, c: Component, deps: Path, cfg, env: dict, vinfo: dict) -> dict:
    d = work / c.path
    sdir = deps / c.slug
    info = {"kind": c.kind, "path": c.path}
    key = digest(c.kind, c.mode, read_all(manifest_files(d, c.kind)), vinfo.get("tools"), vinfo.get("path"),
                 cfg.install_scripts, getattr(runner, "image", ""))  # another image: binaries built again
    if c.kind == "node":
        return await _setup_node(runner, d, c, sdir, key, cfg, env, info)
    if c.kind == "go":
        try:
            needs = re.search(r"(?m)^require\b", (d / "go.mod").read_text(encoding="utf-8")) is not None
        except OSError:
            needs = False
        argv = ["go", "mod", "download"] if needs else None
    elif c.kind == "rust":
        argv = ["cargo", "fetch"]
    else:
        argv = ["dotnet", "restore", *dotnet_target(d)]
    async with _lock(str(sdir)):
        if _stamp_ok(sdir, key):
            return {**info, "reused": True}
        if argv is None:
            _stamp(sdir, key)
            return {**info, "setup": "no dependencies"}
        exe = runner.exe(argv, env)
        if exe is None:
            return {**info, "error": f"{argv[0]} is not installed (or not on PATH)"}
        code, o = await runner.sh(exe, d, env, cfg.setup_timeout)
        if code != 0:
            return {**info, "error": f"{' '.join(argv)} failed (exit {code})", "log": o}
        _stamp(sdir, key)
        return {**info, "setup": f"dependencies fetched with {' '.join(argv)}", "log": o}


async def _setup_node(runner, d: Path, c: Component, sdir: Path, key: str, cfg, env: dict, info: dict) -> dict:
    pm = node_pm(d)
    argv = node_install_argv(d, pm, cfg.install_scripts)
    info["manager"] = pm
    exe = runner.exe(argv, env)
    if c.mode == "inplace":  # workspaces / local file deps: the install needs the sources, so it happens here
        if (d / "node_modules").is_dir():
            return {**info, "setup": "node_modules from the workspace"}
        if exe is None:
            return {**info, "error": f"{pm} is not installed (or not on PATH)"}
        code, o = await runner.sh(exe, d, env, cfg.setup_timeout)
        if code != 0:
            return {**info, "error": f"{' '.join(argv)} failed (exit {code})", "log": o}
        return {**info, "setup": f"installed in this run's copy with {' '.join(argv)} (workspaces or local deps: "
                                 "not kept between runs)", "log": o}
    pkg = sdir / "pkg"
    async with _lock(str(sdir)):
        if _stamp_ok(sdir, key) and (pkg / "node_modules").is_dir():
            info["reused"] = True
        else:
            if exe is None:
                return {**info, "error": f"{pm} is not installed (or not on PATH)"}
            pkg.mkdir(parents=True, exist_ok=True)
            for old in pkg.iterdir():  # only the manifests of now, so a deleted lockfile is gone here too
                if old.name != "node_modules":
                    shutil.rmtree(old, ignore_errors=True) if old.is_dir() and not old.is_symlink() else old.unlink()
            for f in manifest_files(d, "node"):
                shutil.copy2(f, pkg / f.name)
            for extra in ("patches", ".yarn"):  # patch-package / pnpm patches, yarn releases and plugins
                if (d / extra).is_dir():
                    shutil.copytree(d / extra, pkg / extra, ignore=shutil.ignore_patterns("cache", "install-state.gz"))
            code, o = await runner.sh(exe, pkg, env, cfg.setup_timeout)
            if code != 0:
                (sdir / "stamp.json").unlink(missing_ok=True)
                return {**info, "error": f"{' '.join(argv)} failed (exit {code})", "log": o}
            (pkg / "node_modules").mkdir(exist_ok=True)  # no dependencies: still something to link
            _stamp(sdir, key)
            info.update(setup=f"installed with {' '.join(argv)}", log=o)
    nm = d / "node_modules"
    if nm.is_symlink() or nm.is_file():
        nm.unlink()
    elif nm.is_dir():
        shutil.rmtree(nm, ignore_errors=True)
    (runner.link if c.mode == "link" else clone_tree)(pkg / "node_modules", nm)
    info["node_modules"] = "linked" if c.mode == "link" else "cloned"
    return info


# ---- docker images ---------------------------------------------------------------------------------------------------
UV_IMAGE = "astral/uv:0.9.30"           # uv for the venv inside a composite image
COMPOSITE_BASE = "buildpack-deps:bookworm"  # compilers, make, python3, git: what native packages need
# toolchain dirs on PATH in every image we run (missing ones are harmless)
DOCKER_TOOL_PATH = "/usr/local/go/bin:/usr/local/cargo/bin:/usr/share/dotnet"


def rust_channel(work: Path, comps: list[Component]) -> str:
    for c in comps:
        if c.kind != "rust":
            continue
        d = work / c.path
        for f in (d / "rust-toolchain.toml", d / "rust-toolchain", work / "rust-toolchain.toml"):
            if f.is_file():
                text = f.read_text(encoding="utf-8")
                m = re.search(r'channel\s*=\s*"([^"]+)"', text) or re.match(r"\s*(\S+)", text)
                return m.group(1) if m else ""
    return ""


def default_image(kind: str, version: str) -> str:
    """The official image for a toolchain at a version (bookworm variants: they have compilers for native deps)."""
    if kind == "node":
        return f"node:{version or '22'}-bookworm"
    if kind == "go":
        return f"golang:{version}-bookworm" if version else "golang:bookworm"
    if kind == "rust":
        v = version if re.match(r"^\d+(\.\d+){0,2}$", version or "") else "1"  # stable/nightly: the latest 1.x
        return f"rust:{v}-bookworm"
    if kind == "dotnet":
        m = re.match(r"^(\d+\.\d+)", version or "")
        return f"mcr.microsoft.com/dotnet/sdk:{m.group(1) if m else '8.0'}"
    raise ValueError(kind)


def composite_dockerfile(images: dict[str, str], python: bool, base: str) -> str:
    """One image with every toolchain the project uses, copied out of their official images."""
    lines = [f"FROM {base or COMPOSITE_BASE}"]
    if "node" in images:
        n = images["node"]
        lines += [f"COPY --from={n} /usr/local/bin/node /usr/local/bin/node",
                  f"COPY --from={n} /usr/local/lib/node_modules /usr/local/lib/node_modules",
                  "RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm"
                  " && ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx"
                  " && if [ -e /usr/local/lib/node_modules/corepack ]; then"
                  " ln -sf ../lib/node_modules/corepack/dist/corepack.js /usr/local/bin/corepack; fi"]
    if "go" in images:
        lines += [f"COPY --from={images['go']} /usr/local/go /usr/local/go"]
    if "rust" in images:
        lines += [f"COPY --from={images['rust']} /usr/local/rustup /usr/local/rustup",
                  f"COPY --from={images['rust']} /usr/local/cargo /usr/local/cargo",
                  "ENV RUSTUP_HOME=/usr/local/rustup"]
    if "dotnet" in images:
        lines += [f"COPY --from={images['dotnet']} /usr/share/dotnet /usr/share/dotnet",
                  "RUN ln -sf /usr/share/dotnet/dotnet /usr/local/bin/dotnet",
                  "ENV DOTNET_ROOT=/usr/share/dotnet"]
    if python:
        lines += [f"COPY --from={UV_IMAGE} /uv /uvx /usr/local/bin/"]
    lines += [f"ENV PATH=/usr/local/sbin:/usr/local/bin:{DOCKER_TOOL_PATH}:/usr/sbin:/usr/bin:/sbin:/bin"]
    return "\n".join(lines) + "\n"


def docker_plan(work: Path, comps: list[Component], cfg) -> dict:
    """Which image runs this workspace: {"image", "dockerfile" (None: pull as is), "tools": {kind: image}}.
    One toolchain: its official image. Several (or Python plus one): a composite image built once, tagged by its
    Dockerfile. Images can be overridden per toolchain with sandbox.images."""
    kinds = sorted({c.kind for c in comps})
    want = wanted_versions(work, comps)
    want["rust"] = rust_channel(work, comps)
    overrides = getattr(cfg, "images", None) or {}
    tools = {k: overrides.get(k) or default_image(k, want.get(k, "")) for k in kinds if k != "python"}
    if len(tools) == 1 and "python" not in kinds:
        return {"image": next(iter(tools.values())), "dockerfile": None, "tools": tools}
    df = composite_dockerfile(tools, "python" in kinds, getattr(cfg, "composite_base", "") or COMPOSITE_BASE)
    return {"image": f"orch-toolchains:{hashlib.sha256(df.encode()).hexdigest()[:12]}", "dockerfile": df,
            "tools": {**tools, **({"python": "uv"} if "python" in kinds else {})}}
