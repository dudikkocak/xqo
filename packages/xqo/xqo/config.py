from __future__ import annotations

import json
import os
from pathlib import Path

from .agent import Agent
from .artifacts import ArtifactStore
from .auth import Auth, AuthConfig
from .attachments import AttachmentStore
from .avatars import folder_for, from_folder, resolve_avatar
from .compaction import CompactionConfig
from .bus import MessageBus
from .branding import hq_name, resolve_nickname
from .departments import HQ, Budget, load_files, make_department, parse_only, resolve_dir
from .folders import FolderConfig
from .gitstore import GitConfig, GitRepo
from .limits import Limits
from .offer_judge import JudgeConfig, OfferJudge
from .llm import AnthropicLLM, MockLLM, resolve_max_tokens, resolve_model
from .grants import ToolPolicy, expand, resolve as resolve_grant
from .mcp_hub import MCPHub, is_remote
from .migrations import migrate
from .plugin import PluginManager
from .store import open_store
from .providers import Clients, describe, resolve_provider
from .memory import MemoryBoard
from .models import Event
from .sandbox import Sandbox, SandboxConfig
from .lifecycle import TaskLifecycle
from .skills import SkillLibrary
from .usage import UsageTotals

GRAPH_LAYOUTS = ("departments", "ring", "grid", "star")
PACKAGED_CONFIG = Path(__file__).parent / "default_agents.json"


def load_config(config_path: str | Path = "agents.json") -> dict:
    p = Path(config_path)
    if not p.is_file():  # fall back to the config shipped with the package
        p = PACKAGED_CONFIG
    return json.loads(p.read_text(encoding="utf-8"))


def _cfg_dir(config_path: str | Path) -> Path:
    return Path(config_path).parent if Path(config_path).is_file() else PACKAGED_CONFIG.parent


def _sdk_tools(agent: dict, cfg: dict, dept_cfg: dict | None = None) -> list[str] | None:
    """Claude Code tools for a claude-sdk agent: its "sdk_tools", else the department's "default_sdk_tools",
    else agents.json's "default_sdk_tools" (None = the built-in set)."""
    v = agent.get("sdk_tools", (dept_cfg or {}).get("default_sdk_tools", cfg.get("default_sdk_tools")))
    if v is not None and (not isinstance(v, list) or not all(isinstance(x, str) and x for x in v)):
        raise ValueError(f"agent {agent.get('name')!r}: sdk_tools must be a list of Claude Code tool names")
    return v


def _make_agent(a: dict, cfg: dict, ctx: dict, dept: str, servers: dict, sandbox,
                dept_cfg: dict | None = None) -> Agent:
    where = f"agent {a['name']!r}"
    granted, mcp_source = resolve_grant("mcp", "default_mcp", a, dept_cfg, cfg, where)  # grants.py
    for n in granted:  # a name this agent can't see (another department's server) is skipped with a warning
        if n != "all" and n not in servers:
            ctx.setdefault("warnings", []).append(f"{where}: MCP server {n!r} is not available to it")
    mcp_filters = expand(granted, servers, "MCP server", where, strict=False)
    for n, pats in mcp_filters.items():
        if pats and is_remote(servers[n]) and any(c in p for p in pats for c in "*?["):
            raise ValueError(f"{where}: mcp.{n}: list exact tool names for a remote server (no patterns)")
    agent_servers = [servers[n] for n in mcp_filters]
    library = ctx.get("skills")
    packs_granted, packs_source = resolve_grant("skill_packs", "default_skill_packs", a, dept_cfg, cfg, where)
    available = library.available(dept) if library else {}
    for n in packs_granted:
        if n != "all" and n not in available:
            ctx.setdefault("warnings", []).append(f"{where}: skill pack {n!r} is not available to it")
    packs = [available[n] for n in expand(packs_granted, available, "skill pack", where, strict=False)]
    model, effort, source = resolve_model(a, cfg, dept_cfg)
    max_tokens, mt_source = resolve_max_tokens(a, cfg, dept_cfg)
    provider, spec, psource = resolve_provider(a, cfg, dept_cfg)
    agent = Agent(
        name=a["name"], skills=a["skills"], system=a.get("system", "You are a helpful specialist agent."),
        llm=ctx["llm"] or ctx["clients"].get(spec), artifacts=ctx["store"], model=model, effort=effort,
        max_turns=a.get("max_turns", 12), max_tokens=max_tokens, can_run_code=a.get("can_run_code", False),
        mcp_servers=agent_servers, department=dept, sandbox=sandbox,
        write_paths=a.get("write_paths"), sdk_tools=_sdk_tools(a, cfg, dept_cfg))
    agent.mcp_filters, agent.mcp_source = mcp_filters, mcp_source
    agent.skill_packs, agent.skill_packs_source = packs, packs_source
    agent.tool_policy = ToolPolicy.resolve(a, dept_cfg, cfg, where)
    if ctx.get("plugins"):  # per-agent keys of plugins ("channels", "channel_tools", "feeds", …)
        ctx["plugins"].agent_config(agent, a, dept_cfg)
    agent.model_source = source
    agent.max_tokens_source = mt_source
    base = Path(dept_cfg["_source"]).parent if dept_cfg and dept_cfg.get("_source") else _cfg_dir(ctx["config_path"])
    agent.avatar = (resolve_avatar(a.get("avatar"), base, f"agent {a['name']!r}")
                    or from_folder(folder_for(cfg, _cfg_dir(ctx["config_path"])), a["name"]))  # avatars/<name>.png
    agent.nickname = resolve_nickname(a.get("nickname"), f"agent {a['name']!r}")
    agent.provider, agent.provider_type, agent.provider_source = provider, spec["type"], psource
    agent.provider_spec = spec
    return agent


def _check_nicknames(cfg: dict, rows: list[tuple[dict, str]], dept_files: list[dict]) -> None:
    """Nicknames are how people address an agent, so they must be unique (case-insensitive) and must not
    shadow an agent id, a department name or a reserved word."""
    reserved = {n.casefold() for n in ["user", "hq", "all", "*"] + [d["name"] for d in dept_files]}
    ids = {a["name"].casefold() for a, _ in rows}
    seen: dict[str, str] = {}
    labelled = [(a["name"], resolve_nickname(a.get("nickname"), f"agent {a['name']!r}")) for a, _ in rows]
    labelled.append(("the user node", resolve_nickname(cfg.get("user_nickname"), "user_nickname")))
    for who, nick in labelled:
        if not nick:
            continue
        k = nick.casefold()
        if k in ids and who != "the user node" and k == who.casefold():
            continue  # an agent's nickname may repeat its own id (harmless)
        if k in ids or k in reserved:
            raise ValueError(f"nickname {nick!r} of {who} clashes with an agent id, a department or a reserved name")
        if k in seen:
            raise ValueError(f"nickname {nick!r} is used by both {seen[k]} and {who}; nicknames must be unique")
        seen[k] = who


def _roster(cfg: dict, dept_files: list[dict]) -> tuple[list[tuple[dict, str]], list[dict]]:
    """All agent definitions with their department, and every MCP server (global + departments)."""
    rows = [(a, HQ) for a in cfg.get("agents", [])]
    servers = list(cfg.get("mcp_servers", []))
    for d in dept_files:
        rows += [(a, d["name"]) for a in d["agents"]]
        servers += d.get("mcp_servers") or []
    names = [a["name"] for a, _ in rows]
    dup = {n for n in names if names.count(n) > 1}
    if dup:
        raise ValueError(f"agent names must be unique across departments: {', '.join(sorted(dup))}")
    _check_nicknames(cfg, rows, dept_files)
    snames = [s["name"] for s in servers]
    if len(set(snames)) != len(snames):
        raise ValueError("MCP server names must be unique across agents.json and departments")
    if not rows:
        raise ValueError("no agents configured")
    return rows, servers


def _mock_llm(rows: list[tuple[dict, str]], dept_files: list[dict]) -> MockLLM:
    skills = {a["name"]: [s.lower() for s in a["skills"]] for a, _ in rows}
    heads = {d["head"]: [(a["name"], [s.lower() for s in a["skills"]]) for a in d["agents"] if a["name"] != d["head"]]
             for d in dept_files}
    return MockLLM(skills, heads)


def _install_departments(bus: MessageBus, cfg: dict, ctx: dict, dept_files: list[dict]) -> None:
    """(Re)create department objects and their agents on the bus."""
    rows, _ = _roster(cfg, dept_files)
    ctx["skills"] = SkillLibrary.load(_cfg_dir(ctx["config_path"]), cfg, dept_files)  # skills.py
    ctx["warnings"] = []
    global_servers = {s["name"]: s for s in cfg.get("mcp_servers", [])}
    depts = {d["name"]: make_department(d) for d in dept_files}
    sandboxes = {n: Sandbox(SandboxConfig.from_dict({**(cfg.get("sandbox") or {}), **d.sandbox}))
                 for n, d in depts.items() if d.sandbox}
    if ctx.get("mock"):
        m = _mock_llm(rows, dept_files)
        ctx["llm"].skills, ctx["llm"].heads = m.skills, m.heads
    by_dept = {d["name"]: d for d in dept_files}
    new = {}
    for a, dept in rows:
        servers = dict(global_servers)
        if dept != HQ:
            servers.update({s["name"]: s for s in depts[dept].mcp_servers})
        agent = _make_agent(a, cfg, ctx, dept, servers, sandboxes.get(dept), by_dept.get(dept))
        old = bus.agents.get(agent.name)
        if old:  # keep usage counters across reloads; a running old object finishes its current work
            agent.usage = old.usage
        new[agent.name] = agent
    bus.departments = depts
    bus.providers = {}  # providers in use: a safe summary (key variable names and whether they are set)
    for agent in new.values():
        e = bus.providers.setdefault(agent.provider, {**describe(agent.provider_spec), "agents": []})
        e["agents"].append(agent.name)
    bus.agents = {}
    for agent in new.values():
        bus.register(agent)
    bus.skills = ctx["skills"]
    bus.grant_warnings = list(dict.fromkeys(ctx["warnings"]))


def build(config_path: str | Path = "agents.json", artifacts_dir: str = "artifacts",
          log_path: str = "logs/events.jsonl", mock: bool | None = None,
          departments: str | list | None = None, departments_dir: str | Path | None = None,
          run_migrations: bool = True, strict: bool | None = None) -> MessageBus:
    """Build the bus. `departments` picks department files by name ("eng,marketing"; None = all,
    "none" = headquarters only); `departments_dir` overrides where they are read from. `strict`: a plugin error
    stops the build (default: "plugins.strict" in agents.json, or XQO_STRICT=1)."""
    cfg = load_config(config_path)
    limits = Limits.from_config(cfg.get("limits"))  # limits.py: sizes from agents.json + the no-limits hour
    mock = os.getenv("XQO_MOCK") == "1" if mock is None else mock
    folder = Path(departments_dir) if departments_dir else resolve_dir(config_path, cfg)
    only = parse_only(departments)
    dept_files = load_files(folder, only) if only != [] else []
    rows, all_servers = _roster(cfg, dept_files)

    store = ArtifactStore(artifacts_dir)
    applied = migrate(store.root) if run_migrations else []  # migrations.py; stops on data from a newer version
    plugins = PluginManager(None, cfg, strict)  # loads plugins first: they may register sandbox / storage backends
    bus = MessageBus(log_path, store=open_store(cfg.get("storage"), log_path))  # store.py: jsonl, or a backend a plugin registers
    plugins.bus = bus
    bus.plugins = plugins
    bus.migrations_applied = applied
    store.git = GitRepo(GitConfig.from_dict(cfg.get("git")))
    bus.artifacts = store
    bus.limits = limits.persist(store.root / "limits_override.json")
    bus.attachments = AttachmentStore(store.root)
    bus.memory = MemoryBoard(store.root)
    bus.sandbox = Sandbox(SandboxConfig.from_dict(cfg.get("sandbox")))
    bus.mcp = MCPHub(all_servers)
    bus.mcp_remote = [s for s in all_servers if "url" in s]
    bus.budget = Budget(bus, cfg.get("pricing"))
    bus.compaction = CompactionConfig.from_dict(cfg.get("compaction"))
    bus.folders = FolderConfig.from_dict(cfg.get("folders"))
    bus.hq_name = hq_name(cfg.get("name"))
    bus.auth = Auth(AuthConfig.from_dict(cfg.get("auth")), store.root.parent / "auth", bus.hq_name)  # auth.py
    bus.usage_total = UsageTotals(store.root, bus.tasks.values())
    if "attention_timeout" in cfg and "XQO_ATTENTION_TIMEOUT" not in os.environ:
        bus.attention.timeout = float(cfg["attention_timeout"])
    clients = Clients(lambda spec: AnthropicLLM(spec=spec))
    llm = _mock_llm(rows, dept_files) if mock else None
    if not mock:  # titles come from the default provider and model
        _, spec, _ = resolve_provider({}, cfg)
        titler, title_model = clients.get(spec), resolve_model({}, cfg)[0]
        bus.titler = lambda prompt: titler.title(prompt, model=title_model)
    jc = JudgeConfig.from_dict(cfg.get("offer_judge"))
    jcall = None
    if jc.enabled and not mock:  # judged fit: one small call per candidate (offer_judge.py)
        _, jspec, _ = resolve_provider({"provider": jc.provider} if jc.provider else {}, cfg)
        jclient = clients.get(jspec)

        async def jcall(system, messages, model, effort, max_tokens, _c=jclient):
            return await _c.create(model=model, system=system, messages=messages, tools=[], effort=effort,
                                   max_tokens=max_tokens)
    bus.offer_judge = OfferJudge(jc, jcall, resolve_model({}, cfg)[0]) if jc.enabled else None
    bus.department_ctx = {"cfg": cfg, "config_path": str(config_path), "folder": folder, "only": only,
                          "llm": llm, "clients": clients, "store": store, "mock": mock, "plugins": plugins}
    _install_departments(bus, cfg, bus.department_ctx, dept_files)
    bus.config_warnings = list(bus.grant_warnings)
    bus.graph_layout = cfg.get("graph_layout")  # dashboard default; each browser can pick its own
    if bus.graph_layout not in (None, *GRAPH_LAYOUTS):
        raise ValueError(f"graph_layout must be one of {', '.join(GRAPH_LAYOUTS)}")
    bus.user_avatar = _user_avatar(cfg, config_path)
    bus.user_nickname = resolve_nickname(cfg.get("user_nickname"), "user_nickname")  # the user node's label; its id stays "user"
    bus.lifecycle = TaskLifecycle(bus, (cfg.get("trash") or {}).get("retention_days"))  # archive + trash
    bus.lifecycle.purge_expired()
    bus.config_warnings = list(limits.warnings) + list(bus.config_warnings)
    plugins.configure({"bus": bus, "config_path": str(config_path), "data_root": store.root}, cfg)  # plugin.py
    bus.config_warnings = list(dict.fromkeys(bus.config_warnings + plugins.config_warnings()))
    return bus


def _user_avatar(cfg: dict, config_path) -> dict | None:
    return (resolve_avatar(cfg.get("user_avatar"), _cfg_dir(config_path), "user_avatar")
            or from_folder(folder_for(cfg, _cfg_dir(config_path)), "user"))


def reload_departments(bus: MessageBus, only: str | list | None = "keep") -> dict:
    """Re-read agents.json, the department files and the skill packs without a restart. Running work finishes
    on the old agent objects. Local (stdio) MCP servers follow with sync_mcp; remote ones work at once."""
    ctx = bus.department_ctx
    cfg = load_config(ctx["config_path"])
    new_only = ctx["only"] if only == "keep" else parse_only(only)
    dept_files = load_files(ctx["folder"], new_only) if new_only != [] else []  # raises before changing anything
    ctx["only"] = new_only
    before = set(bus.departments)
    _install_departments(bus, cfg, ctx, dept_files)
    bus.user_avatar = _user_avatar(cfg, ctx["config_path"])
    _, servers = _roster(cfg, dept_files)
    bus.mcp_remote = [s for s in servers if is_remote(s)]
    bus.mcp_pending = servers  # local servers: started, restarted or stopped by sync_mcp (web.py awaits it)
    if getattr(bus, "plugins", None):  # the new agents may lack what a plugin needs (feeds access, channels…)
        bus.plugins.recheck()
    after = set(bus.departments)
    summary = {"departments": sorted(after), "added": sorted(after - before), "removed": sorted(before - after),
               "agents": sorted(bus.agents), "skills": sorted(bus.skills.packs), "warnings": bus.grant_warnings}
    bus.emit(Event("departments", "user", None, None, None,
                   f"reloaded {len(after)} department(s)"
                   + (f"; added {', '.join(summary['added'])}" if summary["added"] else "")
                   + (f"; removed {', '.join(summary['removed'])}" if summary["removed"] else ""), summary))
    return summary


async def sync_mcp(bus: MessageBus) -> dict:
    """After reload_departments: start new local MCP servers, restart changed ones, stop removed ones."""
    servers = getattr(bus, "mcp_pending", None)
    if servers is None:
        return {"added": [], "removed": [], "restarted": []}
    bus.mcp_pending = None
    return await bus.mcp.sync(servers)
