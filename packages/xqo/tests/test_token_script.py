"""claude-token-to-env.sh: copy the Claude Code login into .env (the .ps1 twin is not run here)."""
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "claude-token-to-env.sh"
pytestmark = pytest.mark.skipif(not shutil.which("bash") or not shutil.which("python3"), reason="needs bash + python3")
TOKEN = "sk-ant-oat01-" + "A" * 60


def run(tmp_path, *args, expires_in_h=7, token=TOKEN):
    cfg = tmp_path / "cfg"
    cfg.mkdir(exist_ok=True)
    (cfg / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": token, "refreshToken": "sk-ant-ort01-x",
        "expiresAt": int((time.time() + expires_in_h * 3600) * 1000)}}))
    script = tmp_path / "claude-token-to-env.sh"
    shutil.copy(SCRIPT, script)
    return subprocess.run(["bash", str(script), *args], capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin:/usr/local/bin", "CLAUDE_CONFIG_DIR": str(cfg), "HOME": str(tmp_path)})


def test_creates_and_updates_only_the_token_line(tmp_path):
    r = run(tmp_path)
    assert r.returncode == 0 and TOKEN not in r.stdout
    assert (tmp_path / ".env").read_text() == f"CLAUDE_CODE_OAUTH_TOKEN={TOKEN}\n"
    (tmp_path / ".env").write_text("A=1\r\nexport CLAUDE_CODE_OAUTH_TOKEN=old\r\n# c\r\nCLAUDE_CODE_OAUTH_TOKEN=dup\r\nB=2")
    assert run(tmp_path).returncode == 0
    assert (tmp_path / ".env").read_text() == f"A=1\nCLAUDE_CODE_OAUTH_TOKEN={TOKEN}\n# c\nB=2\n"


def test_dry_run_and_expired_do_not_write(tmp_path):
    assert run(tmp_path, "--dry-run").returncode == 0 and not (tmp_path / ".env").exists()
    r = run(tmp_path, expires_in_h=-1)
    assert r.returncode == 1 and "expired" in r.stderr and not (tmp_path / ".env").exists()
    assert run(tmp_path, "--force", expires_in_h=-1).returncode == 0 and (tmp_path / ".env").exists()


def test_missing_login_or_token_fails_clearly(tmp_path):
    r = run(tmp_path, "--file", str(tmp_path / "nope.json"))
    assert r.returncode == 1 and "No Claude Code login" in r.stderr
    r = run(tmp_path, token="")
    assert r.returncode == 1 and "No accessToken" in r.stderr
