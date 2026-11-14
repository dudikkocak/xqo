"""Per-task virtual environments for run_code, always made with uv (local backend and Docker)."""
import asyncio
import shutil
import sys
from pathlib import Path

import pytest

from xqo.config import build
from xqo.sandbox import Sandbox, SandboxConfig, format_result, is_python_project, uv_pip_command

ROOT = Path(__file__).resolve().parent.parent
PREFIX = "python -c 'import sys; print(sys.prefix)'"


def project(tmp_path, reqs="# no deps\n"):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    (ws / "m.py").write_text("print('ok')\n")
    (ws / "requirements.txt").write_text(reqs)
    return ws


@pytest.mark.skipif(not shutil.which("uv"), reason="uv not installed")
def test_uv_venv_created_used_and_reused(tmp_path):
    ws, env = project(tmp_path), tmp_path / "task" / ".venv"
    sb = Sandbox(SandboxConfig(backend="local", timeout=60))
    r = asyncio.run(sb.run(ws, PREFIX, env_dir=env))
    assert r["exit_code"] == 0 and Path(r["stdout"].strip()).resolve() == env.resolve()
    assert r["venv"]["tool"] == "uv" and r["venv"]["created"] and r["venv"]["installed"] == ["requirements.txt"]
    assert "[venv created with uv" in format_result(r)
    r = asyncio.run(sb.run(ws, PREFIX, env_dir=env))  # same deps: nothing reinstalled
    assert not r["venv"]["created"] and r["venv"].get("reused") and r["venv"]["installed"] == []
    (ws / "requirements.txt").write_text("# changed\n")
    r = asyncio.run(sb.run(ws, PREFIX, env_dir=env))
    assert not r["venv"].get("reused") and r["venv"]["installed"] == ["requirements.txt"]


@pytest.mark.skipif(not shutil.which("uv"), reason="uv not installed")
def test_old_venv_tool_values_still_use_uv(tmp_path):
    for tool in ("venv", "auto"):  # python -m venv / pip are never used
        ws, env = project(tmp_path / tool), tmp_path / tool / ".venv"
        r = asyncio.run(Sandbox(SandboxConfig(backend="local", timeout=60, venv_tool=tool)).run(ws, PREFIX, env_dir=env))
        assert r["venv"]["tool"] == "uv" and "uv venv" in r["venv"]["log"] and "-m venv" not in r["venv"]["log"], r
        assert r["exit_code"] == 0 and Path(r["stdout"].strip()).resolve() == env.resolve()


def test_pip_commands_become_uv_pip_on_the_task_venv():
    assert uv_pip_command("pip install matplotlib numpy") == "uv pip install matplotlib numpy"
    assert uv_pip_command("cd x && python -m pip install --user -r req.txt; python m.py") == \
        "cd x && uv pip install -r req.txt; python m.py"
    assert uv_pip_command("pip3 uninstall -y a\npip freeze | grep a") == "uv pip uninstall -y a\nuv pip freeze | grep a"
    assert uv_pip_command("py -m pip install x") == "uv pip install x"  # no interpreter path to quote (cmd.exe)
    for same in ("python m.py", "uv pip install x", "pytest -q", "echo pip install x", "pipx install x"):
        assert uv_pip_command(same) == same


def _wheel(dest: Path) -> Path:
    """A tiny pure-Python wheel, so installing needs no network."""
    import zipfile
    w = dest / "tinypkg-0.1-py3-none-any.whl"
    files = {"tinypkg/__init__.py": "X = 1\n",
             "tinypkg-0.1.dist-info/METADATA": "Metadata-Version: 2.1\nName: tinypkg\nVersion: 0.1\n",
             "tinypkg-0.1.dist-info/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"}
    with zipfile.ZipFile(w, "w") as z:
        for name, body in files.items():
            z.writestr(name, body)
        z.writestr("tinypkg-0.1.dist-info/RECORD", "".join(f"{n},,\n" for n in files) + "tinypkg-0.1.dist-info/RECORD,,\n")
    return w


@pytest.mark.skipif(not shutil.which("uv") or sys.platform == "win32", reason="uv not installed / posix shell")
def test_pip_install_goes_into_task_venv_not_the_outer_one(tmp_path, monkeypatch):
    outer = tmp_path / "outer-venv"  # stands in for xqo's own venv (H:\\xqo\\.venv)
    (outer / "bin").mkdir(parents=True)
    for name in ("pip", "pip3", "python"):
        (outer / "bin" / name).write_text("#!/bin/sh\necho OUTER-$(basename $0) \"$@\"\n")
        (outer / "bin" / name).chmod(0o755)
    monkeypatch.setenv("VIRTUAL_ENV", str(outer))
    monkeypatch.setenv("PATH", f"{outer / 'bin'}:{__import__('os').environ['PATH']}")
    ws, env = project(tmp_path), tmp_path / "task" / ".venv"
    whl = _wheel(tmp_path)
    sb = Sandbox(SandboxConfig(backend="local", timeout=60))
    r = asyncio.run(sb.run(ws, f"pip install {whl} && python -c 'import tinypkg; print(tinypkg.__file__)'", env_dir=env))
    assert r["exit_code"] == 0, r
    assert "OUTER" not in r["stdout"] + r["stderr"] and Path(r["stdout"].strip()).resolve().is_relative_to(env.resolve())
    assert r["ran"] == f"uv pip install {whl} && python -c 'import tinypkg; print(tinypkg.__file__)'"
    assert "[ran as: uv pip install" in format_result(r)
    # pip reached indirectly (not rewritten) is the venv's uv shim; the outer venv is off PATH
    r = asyncio.run(sb.run(ws, "sh -c 'pip uninstall tinypkg' && sh -c 'pip3 freeze'; echo \"$PATH\"", env_dir=env))
    assert r["exit_code"] == 0 and "ran" not in r, r
    assert "OUTER" not in r["stdout"] and "tinypkg" not in r["stdout"] and str(outer / "bin") not in r["stdout"]
    assert r["stdout"].splitlines()[-1].startswith(str(env / "bin") + ":")


def test_quoted_paths_reach_the_command_intact(tmp_path):
    """The command goes to the shell as one string, so its own quotes survive (cmd.exe broke them in a list)."""
    ws = tmp_path / "ws"
    (ws / "a dir").mkdir(parents=True)
    (ws / "a dir" / "f.txt").write_text("hi\n")
    r = asyncio.run(Sandbox(SandboxConfig(backend="local")).run(ws, 'cat "a dir/f.txt" && echo "x  y"'))
    assert r["exit_code"] == 0 and r["stdout"] == "hi\nx  y\n", r


def test_pip_left_alone_without_task_venv(tmp_path):
    ws = project(tmp_path)
    r = asyncio.run(Sandbox(SandboxConfig(backend="local", venv=False)).run(ws, "echo pip; pip --version >/dev/null 2>&1; true",
                                                                         env_dir=tmp_path / "e"))
    assert "ran" not in r and r["command"].startswith("echo pip")


FAKE_DOCKER = """#!/bin/sh
printf '%s\\0' "$@" >> "$XQO_FAKE_DOCKER_LOG"; printf '\\036' >> "$XQO_FAKE_DOCKER_LOG"
"""


def test_uv_required_but_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("xqo.sandbox.shutil.which", lambda name: None)
    ws = project(tmp_path)
    r = asyncio.run(Sandbox(SandboxConfig(backend="local", venv_tool="uv")).run(
        ws, f"{sys.executable} m.py", env_dir=tmp_path / ".venv"))
    assert r["exit_code"] == 0 and "uv is not installed" in r["venv"]["error"]  # runs without a venv


def test_no_venv_when_not_python_or_disabled(tmp_path):
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "a.sh").write_text("echo hi\n")
    assert not is_python_project(tmp_path / "ws")
    r = asyncio.run(Sandbox(SandboxConfig(backend="local")).run(tmp_path / "ws", "sh a.sh", env_dir=tmp_path / "e"))
    assert "venv" not in r and not (tmp_path / "e").exists()
    ws = project(tmp_path / "py")
    r = asyncio.run(Sandbox(SandboxConfig(backend="local", venv=False)).run(ws, "true", env_dir=tmp_path / "e"))
    assert "venv" not in r and not (tmp_path / "e").exists()


def test_tester_uses_task_venv(tmp_path, monkeypatch):
    monkeypatch.setenv("XQO_SANDBOX", "local")
    b = build(ROOT / "agents.json", tmp_path / "art", str(tmp_path / "ev.jsonl"), mock=True)
    t = asyncio.run(b.submit("Build fib and run tests"))
    envs = list((tmp_path / "art").glob(f"{t.root_id}*/.venv"))  # the task's or its department's workspace
    assert envs and all((e.parent / "workspace").is_dir() for e in envs)
    run = next(r for r in b.artifacts.list(t.id) if r["action"] == "run")
    assert "[venv " in b.artifacts.resolve(run["path"]).read_text()
    assert b".venv" not in b.artifacts.zip_task(t.id)
