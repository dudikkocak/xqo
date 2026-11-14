"""Built-in plugins of xqo-lite. Each entry is "name": "module:Class"; installed packages add more through the
``xqo.plugins`` entry point group. See xqo/plugin.py for the interface and PLUGINS.md for the guide.

xqo-lite ships only the office plugin. The other plugins of full xqo are listed in NOT_INCLUDED with the
agents.json keys they read: they show as "missing" in /api/plugins, and a config that still uses one of their
keys (or a plugin that can't be imported) gives a startup warning instead of an error."""

BUILTINS = {
    "office": "xqo.plugins.office:OfficePlugin",            # /office page: 2D and 3D views, editor (on by default)
}

# plugins of full xqo that xqo-lite leaves out -> the agents.json keys they read
NOT_INCLUDED = {
    "schedules": ("schedules",),
    "channels": ("channels",),
    "feeds": ("feeds", "feed_rules"),
    "peering": ("peering",),
    "toolchains": (),
    "docker": (),
    "agency": (),
    "postgres": (),
}
