"""The public API for plugins: import from here, not from the other xqo modules.

    from xqo.api import Plugin, Event, new_id, register_store

Everything listed in __all__ is stable within one plugin API version (API_VERSION, also checked against
Plugin.api when a plugin loads). The rest of the xqo package is private and may change in any release; a change
that breaks something listed here raises API_VERSION and the major version of xqo.
"""
from __future__ import annotations

from . import __version__
from .attachments import AttachmentStore, safe_name, validate as validate_attachments
from .avatars import public as public_avatar, resolve_avatar
from .branding import DEFAULT_TITLE, hq_name, named_page, title
from .bus import Draining, MessageBus
from .client import api as client_api
from .config import build
from .departments import HQ, NAME_RE, PACKAGED_DEPARTMENTS, resolve_dir
from .fences import normalize_path
from .models import Event, Task, new_id, utcnow
from .plugin import API_VERSION, ENTRY_POINT_GROUP, Plugin, PluginContext, quiet_server
from .sandbox import Backend, Sandbox, docker_available, register_backend, use_toolchains
from .store import SEED_KINDS, EventStore, JsonlStore, register_store, store_path
from .web import AttachmentIn, decode_attachments

__all__ = [
    "__version__", "API_VERSION", "ENTRY_POINT_GROUP",
    # plugins
    "Plugin", "PluginContext", "quiet_server",
    # the bus and its records
    "MessageBus", "Draining", "Event", "Task", "new_id", "utcnow",
    # storage backends
    "EventStore", "JsonlStore", "SEED_KINDS", "register_store", "store_path",
    # sandbox backends and toolchains
    "Backend", "Sandbox", "register_backend", "use_toolchains", "docker_available",
    # attachments, paths, departments, branding, avatars
    "AttachmentIn", "decode_attachments", "AttachmentStore", "validate_attachments", "safe_name",
    "normalize_path", "HQ", "NAME_RE", "PACKAGED_DEPARTMENTS", "resolve_dir",
    "DEFAULT_TITLE", "hq_name", "title", "named_page", "public_avatar", "resolve_avatar",
    # building a bus from agents.json, and calling a running server (CLI commands)
    "build", "client_api",
]
