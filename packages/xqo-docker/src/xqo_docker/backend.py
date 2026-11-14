"""Docker sandbox backend (plugin): throwaway containers for run_code, with toolchain images and offline runs.
The core keeps the `local` backend and the shared run/venv machinery."""
from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path, PurePosixPath

from xqo.api import (DOCKER_PATH, Backend, Sandbox, current_toolchains, docker_available, docker_kill, docker_user,
                     is_python_project, mount_hint, net_args, register_backend, setup_net_args, uv_pip_command)

@register_backend
class DockerBackend(Backend):
    """A throwaway container: no network, memory/CPU/pid limits, the host user's uid. Python projects get a uv venv;
    Node/Go/Rust/.NET projects run in their official image (at the version the project asks for), and mixed projects
    in a composite image built once. Dependencies are installed by setup containers (network on, `setup_network`)
    into the task's .deps-docker dir and shared download caches, which runs mount instead of copying."""
    name = "docker"
    toolchains = True
    isolates_network = True

    def available(self) -> bool:
        return docker_available()

    async def command(self, sb, work, command, env_dir, comps, network=False):
        cfg, res, run_cmd = sb.config, {}, command
        image, mounts, env = cfg.image, [], {}
        toolchains = current_toolchains()  # the toolchains plugin, or None
        if toolchains and env_dir is not None and cfg.toolchains and any(c.kind != "python" for c in comps):
            plan = toolchains.docker_plan(work, comps, cfg)
            image = plan["image"]
            info = {"kind": "image", "image": image, "tools": plan["tools"]}
            if plan["dockerfile"]:
                built = await sb.build_image(image, plan["dockerfile"], env_dir.parent / ".deps-docker")
                info.update(built)
            res["toolchains"] = [info]
            if not info.get("error"):
                deps = env_dir.parent / ".deps-docker"
                runner = DockerRunner(sb, image, work, deps, toolchains.cache_root(cfg.cache_dir) / "docker")
                prep = await toolchains.prepare(runner, work, comps, deps, cfg, {"HOME": "/tmp"})
                res["toolchains"] += prep.info
                env = {**prep.env, **({} if network or cfg.network else DOCKER_RUN_OFFLINE)}
                mounts = runner.mounts()
                if prep.managed and (note := toolchains.install_note(command)):
                    res["notes"] = [note]
        container = f"orch-run-{uuid.uuid4().hex[:12]}"  # named, so a cancel can kill it
        argv = ["docker", "run", "--rm", "-i", "--name", container, "--memory", cfg.memory, "--cpus", cfg.cpus,
                "--pids-limit", "256", *docker_user(), "-v", f"{work}:/work", *mounts, "-w", "/work",
                "-e", "HOME=/tmp"]
        argv += [x for k, v in env.items() if k != "HOME" for x in ("-e", f"{k}={v}")]
        if env_dir is not None and cfg.venv and is_python_project(work):
            denv = env_dir.with_name(env_dir.name + "-docker")
            venv = await sb.prepare_docker_env(work, denv, image)
            res["venv"] = venv
            if not venv.get("error"):  # run inside the task's venv; uv run/sync use it too
                argv += ["-v", f"{denv}:/venv", "-e", "VIRTUAL_ENV=/venv/env", "-e", "UV_PROJECT_ENVIRONMENT=/venv/env",
                         "-e", "UV_CACHE_DIR=/venv/cache", "-e", "UV_PYTHON_INSTALL_DIR=/venv/python",
                         "-e", f"PATH=/venv/env/bin:{DOCKER_PATH}"]
                run_cmd = uv_pip_command(command)
        if network:  # approved by the user for this run: the host's CA and proxies, like setup containers
            argv += net_args(cfg)
        elif not cfg.network:
            argv += ["--network", "none"]
        argv += [image, "sh", "-c", run_cmd]
        return {"argv": argv, "env": None, "run_cmd": run_cmd, "container": container, "result": res}


# runs have no network: tools fail fast instead of trying to download
DOCKER_RUN_OFFLINE = {"GOPROXY": "off", "GOTOOLCHAIN": "local", "CARGO_NET_OFFLINE": "true",
                      "npm_config_prefer_offline": "true"}


class DockerRunner:
    """toolchains.prepare's setup commands, each in a throwaway container of the run's image: the run copy at /work,
    the task's deps at /deps, the shared download caches at /cache; network on unless setup_network is false."""
    versions = False   # the image is the version
    host_env = False

    def __init__(self, sb: "Sandbox", image: str, work: Path, deps: Path, cache: Path):
        self.sb, self.image, self.work, self.deps, self.cache = sb, image, work, deps, cache
        deps.mkdir(parents=True, exist_ok=True)  # made by us, not by Docker (it would make them root's)
        cache.mkdir(parents=True, exist_ok=True)

    def deps_view(self, deps: Path):
        return PurePosixPath("/deps")

    def mounts(self) -> list[str]:
        return ["-v", f"{self.deps}:/deps", "-v", f"{self.cache}:/cache"]

    def where(self, p: Path) -> str:
        """A host path under the run copy or the deps dir, as the container sees it."""
        for host, ctr in ((self.work, "/work"), (self.deps, "/deps")):
            try:
                return (PurePosixPath(ctr) / p.relative_to(host).as_posix()).as_posix()
            except ValueError:
                pass
        raise ValueError(f"{p} is not mounted")

    def exe(self, argv: list[str], env: dict) -> list[str]:
        return ["corepack", *argv] if argv[0] in ("pnpm", "yarn") else argv  # node images ship corepack

    def link(self, target: Path, link: Path) -> None:
        os.symlink(self.where(target), link, target_is_directory=True)  # dangling on the host, valid inside

    async def sh(self, argv, cwd: Path, env: dict, timeout: float):
        cfg = self.sb.config
        name = f"orch-setup-{uuid.uuid4().hex[:12]}"
        dargv = ["docker", "run", "--rm", "-i", "--name", name, "--memory", cfg.memory, "--cpus", cfg.cpus,
                 "--pids-limit", "512", *docker_user(), "-v", f"{self.work}:/work", *self.mounts(),
                 *setup_net_args(cfg), "-w", self.where(cwd),
                 *[x for k, v in env.items() for x in ("-e", f"{k}={v}")]]
        if not cfg.setup_network:
            dargv += ["--network", "none"]
        try:
            code, out = await self.sb._sh([*dargv, self.image, *argv], self.work, dict(os.environ), timeout)
        except asyncio.CancelledError:
            await docker_kill(name)
            raise
        if code == -1:
            await docker_kill(name)
        return code, (mount_hint(out) + "\n" + out) if code == 125 and mount_hint(out) else out

