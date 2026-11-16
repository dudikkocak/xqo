"""xqo-toolchains: Node (incl. Nuxt/Next), Go, Rust and .NET for run_code, next to Python's uv venv (core).

It installs itself into the sandbox (`xqo.api.use_toolchains`): component detection, versions through mise,
dependency setup with digest stamps and shared download caches, for the local backend and (with xqo-docker)
toolchain images. Without it (or with `"plugins": {"disable": ["toolchains"]}`) runs still work: Python projects get
their venv, other projects run with whatever is on the host's PATH and no setup step. Per HQ it can also be switched
off with `"sandbox": {"toolchains": false}`. The details are in chains.py."""
from __future__ import annotations

import shutil

from xqo.api import Plugin, use_toolchains

__version__ = "3.1.1"


class ToolchainsPlugin(Plugin):
    name = "toolchains"
    description = "Node / Go / Rust / .NET setup for run_code"

    @classmethod
    def register(cls) -> None:
        from . import chains
        use_toolchains(chains)

    def configure(self, ctx, cfg):
        from . import chains
        on = (cfg.get("sandbox") or {}).get("toolchains", True)
        return {"kinds": [k for k in chains.KINDS if k != "python"], "enabled": bool(on)}

    def status(self) -> dict:
        return {**self.obj, "mise": bool(shutil.which("mise"))}
