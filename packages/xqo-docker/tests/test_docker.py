"""The docker sandbox backend, driven by a fake `docker` script (no daemon needed), plus one real-daemon run."""
import asyncio
import json
import os
import subprocess

import pytest

from xqo.api import Sandbox, SandboxConfig, build, docker_available, mount_hint
from xqo.sandbox import format_result  # not part of the plugin API; used here only to check the report text

posix_only = pytest.mark.skipif(os.name == "nt", reason="the fake docker is a sh script")
PREFIX = "python -c 'import sys; print(sys.prefix)'"
DENIED = ("docker: Error response from daemon: mounts denied:\nThe path /tmp/orch-run-cgm9yxvr/work is not shared "
          "from the host and is not known to Docker.\nYou can configure shared paths from Docker -> Preferences...\n")


def fake_docker(tmp_path, monkeypatch, script):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "docker").write_text("#!/bin/sh\n" + script)
    (bin_ / "docker").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")


def project(tmp_path, reqs="# no deps\n"):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    (ws / "m.py").write_text("print('ok')\n")
    (ws / "requirements.txt").write_text(reqs)
    return ws


def test_plugin_is_found_and_active(tmp_path):
    cfg = tmp_path / "agents.json"
    cfg.write_text(json.dumps({"agents": [{"name": "planner", "skills": ["planning"]}]}))
    bus = build(cfg, tmp_path / "artifacts", str(tmp_path / "logs" / "events.jsonl"), mock=True, departments="none")
    st = bus.plugins.states["docker"]
    assert st.state == "active" and st.source == "xqo_docker:DockerPlugin"
    assert Sandbox(SandboxConfig(backend="docker")).network_requestable


@posix_only
def test_run_with_network_drops_none_and_offline_env(tmp_path, monkeypatch):
    log = tmp_path / "docker.log"
    fake_docker(tmp_path, monkeypatch, f'printf "%s\\n" "$@" > "{log}"\n')
    ca = tmp_path / "ca.pem"
    ca.write_text("x")
    sb = Sandbox(SandboxConfig(backend="docker", venv=False, ca_file=str(ca)))
    asyncio.run(sb.run(tmp_path / "ws", "curl https://example.com", network=True))
    argv = log.read_text().splitlines()
    assert "none" not in argv and f"{ca.resolve()}:/etc/ssl/orch-ca.pem:ro" in argv
    asyncio.run(sb.run(tmp_path / "ws", "curl https://example.com"))
    argv = log.read_text().splitlines()
    assert argv[argv.index("--network") + 1] == "none" and not any("orch-ca" in a for a in argv)


@posix_only
def test_mounts_the_run_dir_copy(tmp_path, monkeypatch):
    log = tmp_path / "docker.log"
    fake_docker(tmp_path, monkeypatch, f'printf "%s\\n" "$@" > "{log}"\n')
    ws = tmp_path / "ws"
    ws.mkdir()
    root = tmp_path / "runs"
    r = asyncio.run(Sandbox(SandboxConfig(backend="docker", run_dir=str(root))).run(ws, "true"))
    mount = next(a for a in log.read_text().splitlines() if a.endswith(":/work"))
    assert mount.startswith(str(root.resolve()) + os.sep + "orch-run-") and r["exit_code"] == 0 and "error" not in r
    assert not list(root.glob("orch-run-*"))  # removed after the run


@posix_only
def test_mounts_denied_reported_with_a_fix(tmp_path, monkeypatch):
    assert "File Sharing" in mount_hint(DENIED)
    fake_docker(tmp_path, monkeypatch, f"cat >&2 <<'X'\n{DENIED}X\nexit 125\n")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "requirements.txt").write_text("# none\n")
    sb = Sandbox(SandboxConfig(backend="docker", run_dir=str(tmp_path / "runs")))
    r = asyncio.run(sb.run(ws, "python -m unittest", env_dir=tmp_path / "task" / ".venv"))
    assert r["exit_code"] == 125 and "File Sharing" in r["error"] and "File Sharing" in r["venv"]["error"]
    out = format_result(r)
    assert "[sandbox ERROR: Docker can't mount" in out and "[venv ERROR: Docker can't mount" in out


FAKE_DOCKER = """printf '%s\\0' "$@" >> "$XQO_FAKE_DOCKER_LOG"; printf '\\036' >> "$XQO_FAKE_DOCKER_LOG"\n"""


@posix_only
def test_venv_made_with_uv_in_the_image(tmp_path, monkeypatch):
    fake_docker(tmp_path, monkeypatch, FAKE_DOCKER)
    log = tmp_path / "docker.log"
    monkeypatch.setenv("XQO_FAKE_DOCKER_LOG", str(log))
    ws, env = project(tmp_path), tmp_path / "task" / ".venv"
    (ws / "tests").mkdir()
    sb = Sandbox(SandboxConfig(backend="docker"))
    r = asyncio.run(sb.run(ws, "pytest -q", env_dir=env))
    calls = lambda: [c.split("\0")[:-1] for c in log.read_text().split("\x1e")[:-1]]  # noqa: E731
    setup, run = calls()
    script = setup[-1]
    assert f"{env}-docker:/venv" in setup and setup[:2] == ["run", "--rm"]
    assert "uv venv --quiet /venv/env" in script and "uv pip install --quiet --python /venv/env/bin/python -r requirements.txt" in script
    assert "pytest" in script and "-m venv" not in script and "\npip " not in script
    assert SandboxConfig().image in setup and "--network" not in setup  # deps may be downloaded during setup
    assert f"{env}-docker:/venv" in run and "VIRTUAL_ENV=/venv/env" in run and run[run.index("none") - 1] == "--network"
    assert r["venv"]["tool"] == "uv" and r["venv"]["created"] and r["venv"]["installed"] == ["requirements.txt"]
    (tmp_path / "task" / ".venv-docker" / "env").mkdir()  # what the image's uv would have made
    r = asyncio.run(sb.run(ws, "pytest -q", env_dir=env))  # same deps: no setup container
    assert r["venv"].get("reused") and len(calls()) == 3
    r = asyncio.run(sb.run(ws, "pip install x", env_dir=env))
    assert calls()[-1][-1] == "uv pip install x" and r["ran"] == calls()[-1][-1] and "VIRTUAL_ENV=/venv/env" in calls()[-1]


IMAGE = SandboxConfig().image


def _image_ready():
    return docker_available() and subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode == 0


@pytest.mark.skipif(not _image_ready(), reason=f"docker or the {IMAGE} image is not available")
def test_real_docker_runs_in_uv_venv(tmp_path):
    ws, env = project(tmp_path), tmp_path / "task" / ".venv"
    r = asyncio.run(Sandbox(SandboxConfig(backend="docker", timeout=60)).run(ws, PREFIX + " && uv --version", env_dir=env))
    assert r["exit_code"] == 0, r
    assert r["stdout"].splitlines()[0] == "/venv/env" and r["stdout"].splitlines()[1].startswith("uv ")
    assert (tmp_path / "task" / ".venv-docker" / "env").is_dir()
