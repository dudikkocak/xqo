"""xqo plugins: optional features (office, schedules, channels, feeds, peering, docker, agency, …) outside the core.

A plugin is a subclass of `Plugin`. The core finds plugins in two places:

* built-ins, listed in `xqo.plugins.BUILTINS` ("name" -> "module:Class"), and
* installed packages that declare an entry point in the group ``xqo.plugins`` (``name = "pkg.module:Class"``).

Nothing is installed from a URL, a chat message or the dashboard.

agents.json decides which plugins run:

    "plugins": {"disable": ["office", "docker"],   # never load these
                "strict": false}                   # true: a plugin error stops startup (like `xqo check`)

Everything else that is available is loaded. A plugin that has nothing to do (e.g. channels without a
"channels" block) returns None from `configure` and stays "inactive": no hooks, no routes, no cost.

Missing plugins: a known plugin that is not installed (plugins.KNOWN, e.g. `pip install xqo-schedules`) or one
whose module can't be imported is "missing". That is always a warning, never an error, also with "strict": true:
the rest of the HQ runs, and the config keys of a missing plugin are ignored.

Plugins import the core only through `xqo.api` (the public API, versioned by API_VERSION); everything else in the
xqo package is private and may change in any release.

Failure isolation: an error while configuring or starting a plugin marks it "failed" (shown in
/api/health, /api/plugins and as a startup warning), disables the plugins that require it, and startup goes on.
With "strict": true, XQO_STRICT=1, or `xqo check`, the same error stops startup instead. Gates fail closed: a
plugin whose `allows` raises denies the tool. Core security (grants, network grants, limits, departments) is not a
plugin and keeps failing hard.
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .bus import MessageBus

API_VERSION = 1
ENTRY_POINT_GROUP = "xqo.plugins"
START_TIMEOUT = float(os.getenv("XQO_PLUGIN_START_TIMEOUT", "15"))
STOP_TIMEOUT = 10.0
EVENT_ERRORS_MAX = 20  # on_task_done / hook errors before a plugin is suspended


class Plugin:
    """Base class: override what the plugin needs; every hook has a do-nothing default."""
    name: str = ""
    description: str = ""
    api: int = API_VERSION
    requires: tuple[str, ...] = ()     # plugins that must be active first (feeds requires schedules)
    service: str | None = None         # the bus attribute that holds this plugin's object (bus.channels, …)
    tool_names: frozenset[str] = frozenset()  # agent tools this plugin executes (call_tool)
    agent_keys: tuple[str, ...] = ()   # per-agent keys in agents.json that this plugin reads (agent_config)
    config_keys: tuple[str, ...] = ()  # top-level agents.json keys this plugin owns ("channels", …)
    static: tuple[str, Path] | None = None  # (URL prefix, folder) of browser files the dashboard serves
    # backups (backup.py) and the pre-migration snapshot: folders under artifacts/ that hold this plugin's runtime
    # settings, file name globs that never go into an archive, files next to agents.json that belong to the config
    state_dirs: tuple[str, ...] = ()
    skip_files: tuple[str, ...] = ()
    config_files: tuple[str, ...] = ()
    nav: dict | None = None            # a link/button the dashboard shows when the plugin is active
    ui: tuple[str, ...] = ()           # browser scripts (in the `static` folder) the dashboard loads when active

    def __init__(self) -> None:
        self.ctx: PluginContext | None = None
        self.obj: Any = None           # the service object returned by configure

    # ---- class-level: no configuration needed (the CLI runs without building the HQ) ----------------
    @classmethod
    def register(cls) -> None:
        """Called once when the plugin is loaded, before the HQ is built: register sandbox backends, channel
        adapters, feed source types, … Must not need a configuration."""

    @classmethod
    def add_cli(cls, sub, url_default) -> list[str]:
        """Add `xqo <command>` sub-commands; return their names."""
        return []

    @classmethod
    def run_cli(cls, a) -> None:
        """Run one of the commands from add_cli (`a.cmd` says which)."""

    # ---- per agent, while the roster is built --------------------------------------------------------
    def agent_config(self, agent, a: dict, dept_cfg: dict | None) -> None:
        """Read this plugin's per-agent keys from the agent's definition `a` and set them on `agent`."""

    # ---- lifecycle --------------------------------------------------------------------------------------
    def configure(self, ctx: "PluginContext", cfg: dict) -> Any:
        """Validate the configuration and build the plugin's object. Return None when there is nothing to do.
        Raise ValueError for a bad configuration: the plugin is marked failed (or startup stops when strict)."""
        return None

    def check_agents(self) -> list[str]:
        """After the roster is built and again after a department reload: warnings about agents."""
        return []

    async def start(self) -> None:
        """Start background work (inside `serve`)."""

    async def stop(self) -> None:
        """Stop background work."""

    def status(self) -> dict | None:
        """Shown under the plugin in /api/plugins and /api/config."""
        return None

    def banner(self) -> list[str]:
        """Lines printed by `serve` at startup."""
        return []

    def side_servers(self) -> list[tuple[Any, str, int]]:
        """Extra listeners for `serve`: (FastAPI app, host, port), e.g. the peer API."""
        return []

    # ---- web ----------------------------------------------------------------------------------------------
    def routes(self, app, bus: "MessageBus") -> None:
        """Add this plugin's HTTP routes to the dashboard app."""

    # ---- agent hooks (one dispatcher calls these for the API agent loop AND the Claude Agent SDK loop) ------
    def tool_defs(self, agent) -> list[dict]:
        return []

    async def call_tool(self, task, agent, name: str, inp: dict) -> str:
        raise KeyError(name)

    def allows(self, task, agent, tool: str) -> bool:
        """Gate for every tool call, Claude Code tool and remote MCP server ("mcp:<name>") of a task."""
        return True

    def deny_message(self, tool: str) -> str:
        return f"{tool} is not available in this task ({self.name} plugin)"

    def filter_tools(self, task, agent, tools: list[dict], servers: list[dict]) -> tuple[list[dict], list[dict]]:
        return tools, servers

    def prompt_note(self, agent) -> str:
        return ""

    async def on_task_done(self, task) -> None:
        """A root task finished (done, error or cancelled)."""


@dataclass
class PluginContext:
    """What a plugin gets from the core: the bus, its own data folder and the configuration (read-only use)."""
    bus: "MessageBus"
    cfg: dict
    config_path: str
    data_root: Path
    manager: "PluginManager"

    def data_dir(self, name: str) -> Path:
        return self.data_root / "plugins" / name

    def get(self, name: str) -> Any:
        """The object of another active plugin (None if it is not active)."""
        return self.manager.service(name)


@dataclass
class PluginState:
    name: str
    state: str = "loaded"      # loaded | active | inactive | failed | disabled | suspended | missing
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    description: str = ""
    source: str = "builtin"    # builtin | entry point "pkg.module:Class"
    errors: int = 0
    started: bool = False

    def to_dict(self) -> dict:
        return {"name": self.name, "state": self.state, "error": self.error, "warnings": self.warnings,
                "description": self.description, "source": self.source}


# ---- what installed plugins add to backups (filled by load_classes, read by backup.py and migrations.py) ----
STATE_DIRS: set[str] = set()
SKIP_FILES: set[str] = set()
CONFIG_FILES: set[str] = set()


def _collect(cls: type[Plugin]) -> None:
    STATE_DIRS.update(cls.state_dirs)
    SKIP_FILES.update(cls.skip_files)
    CONFIG_FILES.update(cls.config_files)


# ---- discovery ----------------------------------------------------------------------------------------------
def _load_ref(ref: str) -> type[Plugin]:
    mod, _, attr = ref.partition(":")
    cls = getattr(importlib.import_module(mod), attr or "PLUGIN")
    if not (isinstance(cls, type) and issubclass(cls, Plugin)):
        raise TypeError(f"{ref} is not a xqo.plugin.Plugin subclass")
    return cls


def available() -> dict[str, str]:
    """name -> "module:Class" of every plugin this installation has (built-ins, then entry points)."""
    from .plugins import BUILTINS
    out = dict(BUILTINS)
    try:
        from importlib.metadata import entry_points
        for ep in entry_points(group=ENTRY_POINT_GROUP):
            out.setdefault(ep.name, ep.value)
    except Exception:  # broken metadata must never stop the core
        pass
    return out


def load_classes(disabled: set[str] | frozenset[str] = frozenset(),
                 errors: dict[str, str] | None = None,
                 missing: dict[str, str] | None = None) -> dict[str, type[Plugin]]:
    """Import the available plugins (except `disabled`) and call their `register`. Errors go to `errors`; a plugin
    whose module isn't there (ModuleNotFoundError for the plugin's own module) goes to `missing` when given."""
    out: dict[str, type[Plugin]] = {}
    for name, ref in available().items():
        if name in disabled:
            continue
        try:
            cls = _load_ref(ref)
        except ModuleNotFoundError as e:
            mod = ref.partition(":")[0]
            if missing is not None and e.name and (mod == e.name or mod.startswith(e.name + ".")):
                missing[name] = f"module {e.name} is not installed"
                continue
            if errors is not None:
                errors[name] = f"{type(e).__name__}: {e}"
            continue
        except Exception as e:
            if errors is not None:
                errors[name] = f"{type(e).__name__}: {e}"
            continue
        try:
            if cls.api != API_VERSION:
                raise RuntimeError(f"needs plugin API {cls.api}, this xqo has {API_VERSION}")
            cls.register()
            _collect(cls)
            out[name] = cls
        except Exception as e:
            if errors is not None:
                errors[name] = f"{type(e).__name__}: {e}"
    return out


def quiet_server(app, host: str, port: int):
    """A uvicorn server for a side listener (webhooks, push feeds) that leaves signals to the main server."""
    import uvicorn

    class Server(uvicorn.Server):
        def install_signal_handlers(self):
            pass

        @contextlib.contextmanager
        def capture_signals(self):
            yield

    return Server(uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False))


# ---- the manager ------------------------------------------------------------------------------------------------
class PluginManager:
    """Loads, configures, starts and stops plugins; the single dispatcher for their hooks."""

    def __init__(self, bus: "MessageBus", cfg: dict, strict: bool | None = None):
        self.bus = bus
        pcfg = cfg.get("plugins") or {}
        if not isinstance(pcfg, dict):
            raise ValueError('"plugins" must be an object like {"disable": ["office"]}')
        unknown = set(pcfg) - {"disable", "strict"}
        if unknown:
            raise ValueError(f"plugins: unknown setting(s) {', '.join(sorted(unknown))}; known: disable, strict")
        env = os.getenv("XQO_STRICT")
        self.strict = bool(pcfg.get("strict")) if strict is None else strict
        if env is not None and strict is None:
            self.strict = env not in ("", "0", "false")
        disabled = pcfg.get("disable") or []
        if not isinstance(disabled, list) or not all(isinstance(x, str) for x in disabled):
            raise ValueError('plugins.disable must be a list of plugin names')
        self.disabled = set(disabled)
        self.cfg = cfg
        from .plugins import KNOWN
        names = available()
        not_installed = {n: v for n, v in KNOWN.items() if n not in names}
        bad = self.disabled - set(names) - set(not_installed)
        self.warnings: list[str] = [f"plugins.disable: no plugin named {n!r}" for n in sorted(bad)]
        self.states: dict[str, PluginState] = {}
        for n in names:
            self.states[n] = PluginState(n, "disabled" if n in self.disabled else "loaded", source=
                                         "builtin" if names[n].startswith("xqo.plugins.") else names[n])
        for n, (pkg, keys) in not_installed.items():  # known, published separately: listed, never loaded
            self.states[n] = PluginState(n, "missing", f"not installed (pip install {pkg})", source=pkg)
            used = [k for k in keys if k in cfg]
            if used and n not in self.disabled:
                self._missing(n, f"is not installed (pip install {pkg}); {', '.join(repr(k) for k in used)} "
                                 f"in agents.json is ignored")
        errors: dict[str, str] = {}
        missing: dict[str, str] = {}
        classes = load_classes(self.disabled, errors, missing)
        for n, why in missing.items():
            self.states[n].state, self.states[n].error = "missing", why
            self._missing(n, f"is missing ({why}); it is off")  # an entry point whose package is broken or gone
        for n, err in errors.items():
            self._fail(n, f"could not be loaded: {err}")
        self.plugins: dict[str, Plugin] = {}
        for n in self._order(classes):
            p = classes[n]()
            p.name = p.name or n
            self.plugins[n] = p
            self.states[n].description = p.description

    # ---- ordering and failures ----
    def _order(self, classes: dict[str, type[Plugin]]) -> list[str]:
        """Dependency order; a plugin whose requirement is missing, disabled or failed is failed too."""
        done: list[str] = []
        visiting: set[str] = set()

        def visit(n: str) -> bool:
            if n in done:
                return True
            if n not in classes:
                return False
            if n in visiting:
                self._fail(n, "circular requirement")
                return False
            visiting.add(n)
            for r in classes[n].requires:
                if not visit(r):
                    visiting.discard(n)
                    if r not in self.states or self.states[r].state == "missing":  # a warning, never an error
                        self.states[n].state, self.states[n].error = "missing", f"requires the {r} plugin"
                        if any(k in self.cfg for k in classes[n].config_keys):
                            from .plugins import pip_name
                            hint = f" (pip install {pip_name(r)})" if pip_name(r) else ""
                            self._missing(n, f"requires the {r} plugin, which is missing{hint}; it is off")
                        return False
                    why = ("is disabled" if r in self.disabled else "is not installed" if r not in self.states
                           else "failed")
                    if any(k in self.cfg for k in classes[n].config_keys):  # configured: say why it is off
                        self._fail(n, f"requires the {r} plugin, which {why}")
                    else:  # not used anyway: off without a warning
                        self.states[n].state, self.states[n].error = "inactive", f"requires the {r} plugin"
                    return False
            visiting.discard(n)
            done.append(n)
            return True

        for n in sorted(classes):
            visit(n)
        return done

    def _missing(self, name: str, text: str) -> None:
        """A missing plugin is a warning, never an error (also in strict mode); the rest of the HQ runs."""
        msg = f"plugin {name} {text}"
        self.warnings.append(msg)

    def _fail(self, name: str, error: str) -> None:
        msg = f"plugin {name}: {error}"
        if self.strict:
            raise ValueError(msg)
        st = self.states.setdefault(name, PluginState(name))
        st.state, st.error = "failed", error
        self.warnings.append(msg + " (the plugin is off; the rest of the HQ runs)")
        p = getattr(self, "plugins", {}).pop(name, None)
        if p is not None:
            self._detach(p)
            for other in list(self.plugins.values()):  # plugins that require it go too
                if name in other.requires:
                    self._fail(other.name, f"requires the {name} plugin, which failed")

    def _detach(self, p: Plugin) -> None:
        if p.service and getattr(self.bus, p.service, None) is p.obj:
            setattr(self.bus, p.service, None)
        p.obj = None

    def _hook_error(self, p: Plugin, where: str, e: BaseException) -> None:
        st = self.states[p.name]
        st.errors += 1
        line = f"{where}: {type(e).__name__}: {e}"
        st.warnings = (st.warnings + [line])[-10:]
        print(f"plugin {p.name}: {line}", file=sys.stderr)
        if st.errors >= EVENT_ERRORS_MAX and st.state == "active":
            st.state = "suspended"
            st.error = f"suspended after {st.errors} errors (last: {line})"

    # ---- queries ----
    def active(self) -> list[Plugin]:
        return [p for p in self.plugins.values() if self.states[p.name].state == "active"]

    def get(self, name: str) -> Plugin | None:
        p = self.plugins.get(name)
        return p if p and self.states[name].state == "active" else None

    def service(self, name: str) -> Any:
        p = self.get(name)
        return p.obj if p else None

    def is_active(self, name: str) -> bool:
        return self.get(name) is not None

    # ---- build-time phases ----
    def agent_config(self, agent, a: dict, dept_cfg: dict | None) -> None:
        for p in list(self.plugins.values()):
            if self.states[p.name].state in ("failed", "disabled"):
                continue
            try:
                p.agent_config(agent, a, dept_cfg)
            except Exception as e:
                self._fail(p.name, f"agent {a.get('name')!r}: {e}")

    def configure(self, ctx_args: dict, cfg: dict) -> None:
        for p in list(self.plugins.values()):
            if p.name not in self.plugins:  # failed through a requirement meanwhile
                continue
            p.ctx = PluginContext(manager=self, cfg=cfg, **ctx_args)
            try:
                p.obj = p.configure(p.ctx, cfg)
            except Exception as e:
                if p.service:  # a half-configured service never stays on the bus
                    setattr(self.bus, p.service, None)
                self._fail(p.name, str(e) if isinstance(e, ValueError) else f"{type(e).__name__}: {e}")
                continue
            st = self.states[p.name]
            if p.obj is None:
                st.state = "inactive"
                for other in list(self.plugins.values()):
                    if p.name in other.requires:
                        self._fail(other.name, f"requires the {p.name} plugin, which is not configured")
                continue
            st.state = "active"
            if p.service:
                setattr(self.bus, p.service, p.obj)
            self.check_agents(p)

    def check_agents(self, only: Plugin | None = None) -> list[str]:
        out = []
        for p in ([only] if only else self.active()):
            try:
                w = list(p.check_agents() or [])
            except Exception as e:
                self._fail(p.name, str(e) if isinstance(e, ValueError) else f"{type(e).__name__}: {e}")
                continue
            self.states[p.name].warnings = w
            out += w
        return out

    def recheck(self) -> list[str]:
        """After a department reload: problems become warnings (the new roster is already in use)."""
        out = []
        for p in self.active():
            try:
                w = list(p.check_agents() or [])
            except Exception as e:
                w = [str(e)]
                if hasattr(p.obj, "warnings"):
                    p.obj.warnings = w
            self.states[p.name].warnings = w
            out += w
        return out

    def config_warnings(self) -> list[str]:
        return list(dict.fromkeys(self.warnings + [w for p in self.active() for w in self.states[p.name].warnings]))

    # ---- lifecycle ----
    async def start(self) -> None:
        for p in self.active():
            try:
                await asyncio.wait_for(p.start(), START_TIMEOUT)
                self.states[p.name].started = True
            except Exception as e:  # includes the timeout
                why = f"did not start within {START_TIMEOUT:g}s" if isinstance(e, asyncio.TimeoutError) else \
                    f"start failed: {type(e).__name__}: {e}"
                if self.strict:
                    raise
                self.states[p.name].state, self.states[p.name].error = "failed", why
                self.warnings.append(f"plugin {p.name}: {why}")
                print(f"warning: plugin {p.name}: {why}", file=sys.stderr)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(p.stop(), STOP_TIMEOUT)
                self._detach(p)

    async def stop(self) -> None:
        for p in reversed(list(self.plugins.values())):
            st = self.states[p.name]
            if not st.started:
                continue
            try:
                await asyncio.wait_for(p.stop(), STOP_TIMEOUT)
            except Exception as e:
                print(f"warning: plugin {p.name}: stop failed: {type(e).__name__}: {e}", file=sys.stderr)
            st.started = False

    # ---- agent hooks ----
    def tool_defs(self, agent) -> list[dict]:
        out = []
        for p in self.active():
            try:
                out += p.tool_defs(agent) or []
            except Exception as e:
                self._hook_error(p, "tool_defs", e)
        return out

    def owner(self, tool: str) -> Plugin | None:
        return next((p for p in self.active() if tool in p.tool_names), None)

    async def call_tool(self, task, agent, name: str, inp: dict) -> str:
        p = self.owner(name)
        if p is None:
            raise KeyError(name)
        return await p.call_tool(task, agent, name, inp)

    def denial(self, task, agent, tool: str) -> str | None:
        """None when every active plugin allows `tool` in this task, else the first plugin's message."""
        for p in self.active():
            try:
                ok = p.allows(task, agent, tool)
            except Exception as e:  # fail closed
                self._hook_error(p, "allows", e)
                return f"{tool} is blocked: the {p.name} plugin could not check it"
            if not ok:
                return p.deny_message(tool)
        return None

    def allows(self, task, agent, tool: str) -> bool:
        return self.denial(task, agent, tool) is None

    def filter_tools(self, task, agent, tools: list[dict], servers: list[dict]) -> tuple[list[dict], list[dict]]:
        for p in self.active():
            try:
                tools, servers = p.filter_tools(task, agent, tools, servers)
            except Exception as e:  # fail closed: no tools for this call
                self._hook_error(p, "filter_tools", e)
                return [], []
        return tools, servers

    def prompt_notes(self, agent) -> str:
        out = ""
        for p in self.active():
            try:
                note = p.prompt_note(agent)
            except Exception as e:
                self._hook_error(p, "prompt_note", e)
                continue
            if note:
                out += note + "\n\n"
        return out

    async def task_done(self, task) -> None:
        for p in self.active():
            try:
                await p.on_task_done(task)
            except Exception as e:
                self._hook_error(p, "on_task_done", e)

    # ---- reporting ----
    def describe(self) -> list[dict]:
        out = []
        for n, st in sorted(self.states.items()):
            d = st.to_dict()
            p = self.plugins.get(n)
            if p is not None and st.state == "active":
                try:
                    d["status"] = p.status()
                except Exception as e:
                    d["status"] = {"error": f"{type(e).__name__}: {e}"}
                d["nav"] = p.nav
                d["ui"] = [f"{p.static[0]}/{f}" for f in p.ui] if p.static else []
            out.append(d)
        return out

    def health(self) -> dict:
        return {n: st.state + (f": {st.error}" if st.error else "") for n, st in sorted(self.states.items())}


