"""Where run copies go (sandbox.run_dir) and the hint for Docker's "mounts denied" error."""
import asyncio
import os
import time
from pathlib import Path

import pytest

from xqo.sandbox import Sandbox, SandboxConfig, format_result, mount_hint

DENIED = ("docker: Error response from daemon: mounts denied:\nThe path /tmp/orch-run-cgm9yxvr/work is not shared "
          "from the host and is not known to Docker.\nYou can configure shared paths from Docker -> Preferences...\n")


def fake_docker(tmp_path, monkeypatch, script):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "docker").write_text("#!/bin/sh\n" + script)
    (bin_ / "docker").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")


def test_run_dir_from_config_and_env(tmp_path, monkeypatch):
    monkeypatch.delenv("XQO_RUN_DIR", raising=False)
    assert SandboxConfig.from_dict({"run_dir": "/x"}).run_dir == "/x"
    monkeypatch.setenv("XQO_RUN_DIR", str(tmp_path))
    assert SandboxConfig.from_dict({"run_dir": "/x"}).run_dir == str(tmp_path)


def test_default_run_root_is_temp_locally_and_under_home_with_docker(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    sb = Sandbox(SandboxConfig())
    assert sb.run_root("local") is None
    root = sb.run_root("docker")
    assert root == (tmp_path / ".cache" / "xqo" / "runs").resolve() and root.is_dir()
    custom = tmp_path / "runs"
    assert Sandbox(SandboxConfig(run_dir=str(custom))).run_root("local") == custom.resolve()


def test_stale_run_copies_are_swept(tmp_path):
    root = tmp_path / "sweep"
    old, new = root / "orch-run-old", root / "orch-run-new"
    for d in (old, new):
        (d / "work").mkdir(parents=True)
    os.utime(old, (time.time() - 3 * 86400,) * 2)
    Sandbox(SandboxConfig(run_dir=str(root))).run_root("docker")
    assert not old.exists() and new.exists()


def test_mount_hint():
    h = mount_hint(DENIED)
    assert "/tmp/orch-run-cgm9yxvr/work" in h and "File Sharing" in h and "XQO_RUN_DIR" in h
    assert mount_hint("some other failure") == ""
