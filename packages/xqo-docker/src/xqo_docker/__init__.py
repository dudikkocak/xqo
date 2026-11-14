"""xqo-docker: the "docker" sandbox backend for run_code (throwaway containers, no network unless granted,
toolchain images). With it installed, "sandbox": {"backend": "auto"} picks Docker when the daemon answers; without it
(or with "plugins": {"disable": ["docker"]}) runs use the local backend."""
from __future__ import annotations

import importlib

from xqo.api import Plugin, docker_available

__version__ = "3.0.0"


class DockerPlugin(Plugin):
    name = "docker"
    description = "Docker sandbox backend for run_code"

    @classmethod
    def register(cls) -> None:
        importlib.import_module(f"{__name__}.backend")  # registers "docker" with register_backend

    def configure(self, ctx, cfg):
        return {"backend": "docker"}

    def status(self) -> dict:
        return {**self.obj, "daemon": docker_available()}
