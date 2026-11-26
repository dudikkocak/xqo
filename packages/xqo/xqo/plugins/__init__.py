"""Built-in plugins of xqo. Each entry is "name": "module:Class"; installed packages add more through the
``xqo.plugins`` entry point group. See xqo/plugin.py for the interface and PLUGINS.md for the guide.

The base package ships only the office plugin. The other plugins are separate packages on PyPI; KNOWN lists them
with their pip name and the agents.json keys they read. A known plugin that is not installed shows as "missing"
in /api/plugins, and a config that still uses its keys gives a startup warning (with the pip command), never an
error."""

BUILTINS = {
    "office": "xqo.plugins.office:OfficePlugin",            # /office page: 2D and 3D views, editor (on by default)
}

# plugins published as their own packages -> (pip name, agents.json keys they read)
KNOWN = {
    "schedules": ("xqo-schedules", ("schedules",)),
    "channels": ("xqo-channels", ("channels",)),
    "feeds": ("xqo-feeds", ("feeds", "feed_rules")),
    "peering": ("xqo-peering", ("peering",)),
    "flows": ("xqo-flows", ("flows",)),
    "flows_runner": ("xqo-flows-runner", ("flows_worker",)),
    "toolchains": ("xqo-toolchains", ()),
    "docker": ("xqo-docker", ()),
    "agency": ("xqo-agency", ()),
    "postgres": ("xqo-postgres", ()),
    "sqlite": ("xqo-sqlite", ()),
}


def pip_name(plugin: str) -> str:
    """The package that provides a known plugin ("" for unknown ones)."""
    return KNOWN.get(plugin, ("", ()))[0]
