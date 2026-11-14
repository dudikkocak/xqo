> **xqo-lite.** This build has only the core, the office plugin and the Claude Agent SDK, and stores events in
> jsonl. Parts about schedules, channels, feeds, peering, toolchains, the Docker sandbox, agency, sqlite or
> postgres don't apply here (see README.md). Missing plugins are startup warnings, not errors.

# Writing xqo plugins

A plugin is a subclass of `xqo.plugin.Plugin`. Every hook has a do-nothing default, so a plugin implements only
what it needs. The built-ins in `xqo/plugins/` are the examples: `peering` (tools, routes, CLI, a side listener)
is the most complete, `docker` (only `register`) and `agency` (only CLI) are the smallest.

## Discovery

- Built-ins: `xqo/plugins/__init__.py` → `BUILTINS = {"name": "module:Class"}`.
- Installed packages: an entry point in the group `xqo.plugins`:

```toml
# pyproject.toml of your package
[project.entry-points."xqo.plugins"]
jira = "xqo_jira:JiraPlugin"
```

Nothing is ever installed from a URL, a chat message or the dashboard.

## The interface

```python
from xqo.plugin import Plugin

class JiraPlugin(Plugin):
    name = "jira"
    description = "Jira issues as tasks"
    requires = ("schedules",)          # loaded after these; off (with a reason) when one is off
    service = "jira"                   # bus.jira = the object configure returns
    config_keys = ("jira",)            # top-level agents.json keys it owns
    agent_keys = ("jira",)             # per-agent keys it reads in agent_config
    tool_names = frozenset({"jira_search"})
    static = None                      # ("/plugins/jira", Path(...)) to serve browser files
    nav = None                         # {"id", "label", "href", "title"}: a header link (when it has an href)
    ui = ()                            # ("ui.js",): scripts in `static` the dashboard loads while active

    @classmethod
    def register(cls): ...             # at load time, no config: register backends / adapters / sources
    @classmethod
    def add_cli(cls, sub, url): ...    # add `xqo <cmd>` parsers, return their names
    @classmethod
    def run_cli(cls, a): ...

    def agent_config(self, agent, a, dept_cfg): ...   # read per-agent keys onto the Agent
    def configure(self, ctx, cfg):     # return the plugin's object, or None = "inactive" (nothing to do)
        if "jira" not in cfg:
            return None
        return JiraClient(cfg["jira"], data=ctx.data_dir(self.name))
    def check_agents(self): return []  # warnings; also called after `departments/reload`
    async def start(self): ...         # inside `serve`, in dependency order, with a timeout
    async def stop(self): ...
    def status(self): return {}        # shown in /api/plugins
    def banner(self): return []        # lines printed by `serve`
    def side_servers(self): return []  # [(fastapi_app, host, port)] extra listeners
    def routes(self, app, bus): ...    # add FastAPI routes

    # agent hooks: the same dispatcher serves the API loop and the Claude Agent SDK loop
    def tool_defs(self, agent): return []
    async def call_tool(self, task, agent, name, inp): ...
    def allows(self, task, agent, tool): return True     # gate every tool, Claude Code tool and "mcp:<server>"
    def deny_message(self, tool): ...
    def filter_tools(self, task, agent, tools, servers): return tools, servers
    def prompt_note(self, agent): return ""
    async def on_task_done(self, task): ...              # a root task finished
```

`ctx` (`PluginContext`) gives `ctx.bus`, `ctx.cfg`, `ctx.config_path`, `ctx.data_root`, `ctx.data_dir(name)`
and `ctx.get("other_plugin")`.

## Failure isolation

| What fails | What happens |
|---|---|
| import / `register` / API version mismatch | plugin **failed**, warning, startup goes on |
| `configure` raises (use `ValueError` for config mistakes) | **failed**; plugins that require it are off too, with the reason |
| `check_agents` raises at startup | **failed**; after a department reload it becomes a warning instead |
| `start` raises or exceeds `XQO_PLUGIN_START_TIMEOUT` (15 s) | **failed** and stopped; its object leaves the bus, so its routes answer as if it were not configured |
| `allows` raises | the tool is **denied** (fail closed) |
| `filter_tools` raises | the model call gets no tools (fail closed) |
| `tool_defs`, `prompt_note`, `on_task_done` raise | logged and counted; 20 errors suspend the plugin |
| `"strict": true`, `XQO_STRICT=1`, `xqo check` | any of the above stops startup instead |

## Dashboard UI

The dashboard (`xqo/static/index.html`, `app.css`, `js/*.js`) reads `/api/plugins` at start-up. For each **active**
plugin it adds the `nav` link (if it has an `href`) and loads the plugin's `ui` scripts from its `static` folder.
A disabled, failed or unconfigured plugin's scripts are never requested. A UI script is a plain script that shares
the dashboard's globals (`$`, `esc`, `api`, `dialog`, `toast`, `debounce`, `state`, ...) and uses `XQO`:

```js
XQO.button({id: "tjira", label: "🧩 jira", title: "Jira issues", place: "tasks",  // "header" (default) or "tasks"
            onclick: () => dialog("Jira", "<div id='jbody'></div>", "jira")});
XQO.on(e => { if (e.kind.startsWith("jira_")) toast(e.content, null) });         // every live event
XQO.plugins.jira.status                                                         // its /api/plugins row
```

`xqo/plugins/schedules/static/ui.js`, `feeds/static/ui.js` and `peering/static/ui.js` are the examples. An error
thrown in an `XQO.on` handler is logged in the browser console and does not stop the other handlers.

## Smaller extension points

- **Sandbox backend**: subclass `xqo.sandbox.Backend`, decorate with `@register_backend` in your plugin's
  `register()`, then `"sandbox": {"backend": "<name>"}` (see `xqo/plugins/docker/`).
- **Channel adapter**: subclass `xqo.plugins.channels.base.Adapter` and declare an entry point in the group
  `xqo.channels` (`discord = "pkg:DiscordAdapter"`); use it with `"channels": {"chat": {"type": "discord"}}`. The
  channels hub keeps the access modes and the responder tool filter, so an adapter cannot widen them.
- **Storage backend**: call `xqo.store.register_store("name", factory)` in your plugin's `register()`, where
  `factory(cfg, log_path)` returns an `EventStore` (`append`, `recent`, `seeds`, `tree`, `by_kind_prefix`, `purge`,
  `iter_all`, `count`, `backup_files`, `describe`, `close`). `xqo/plugins/postgres/` is the example, and
  `tests/test_store.py` is the contract every backend must pass.
- **Toolchains**: the sandbox calls whatever module was given to `xqo.sandbox.use_toolchains(module)`
  (`detect`, `prepare`, `copy_ignore`, `describe`, `walk`, `is_python_project`); `xqo/plugins/toolchains/` does that.
