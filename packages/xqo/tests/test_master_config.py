"""One file for everything: a "server" block in agents.json and `xqo <file>.json [command]`."""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _cfg(tmp_path, server):
    cfg = json.loads((ROOT / "agents.json").read_text())
    cfg["server"] = server
    cfg["git"] = {"enabled": False}
    d = tmp_path / "hq"
    d.mkdir()
    p = d / "hq.json"
    p.write_text(json.dumps(cfg))
    return p


def _orch(*args, cwd, env=None):
    e = {k: v for k, v in os.environ.items() if not k.startswith("XQO_")}
    e.update({"XQO_SANDBOX": "off", **(env or {})})
    return subprocess.run([sys.executable, "-m", "xqo", *args], cwd=cwd, env=e, capture_output=True,
                          text=True, timeout=120)


def test_config_file_as_first_argument_uses_its_server_block(tmp_path):
    p = _cfg(tmp_path, {"data_dir": "state", "mock": True, "departments": ["engineering"]})
    r = _orch(str(p), "run", "Build fib", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "Departments: engineering (head eng_lead)\n" in r.stdout
    assert (tmp_path / "hq" / "state" / "artifacts").is_dir()       # relative to the config file, not the cwd
    assert (tmp_path / "hq" / "state" / "logs" / "events.jsonl").is_file()


def test_flags_and_environment_win_over_the_file(tmp_path):
    p = _cfg(tmp_path, {"data_dir": "state", "mock": True})
    r = _orch(str(p), "run", "Build fib", cwd=tmp_path, env={"XQO_DATA": str(tmp_path / "envdata")})
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "envdata" / "artifacts").is_dir() and not (tmp_path / "hq" / "state").exists()
    r = _orch("--config", str(p), "--data-dir", str(tmp_path / "flag"), "--departments", "engineering", "run", "Build fib",
              cwd=tmp_path, env={"XQO_DATA": str(tmp_path / "envdata")})
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "flag" / "artifacts").is_dir() and "Departments: engineering (head eng_lead)\n" in r.stdout


def test_client_commands_find_the_server_from_the_file(tmp_path):
    p = _cfg(tmp_path, {"host": "0.0.0.0", "port": 8765})
    r = _orch(str(p), "drain", cwd=tmp_path)
    assert "no server answering at http://127.0.0.1:8765" in r.stderr


def test_unknown_server_setting_is_an_error(tmp_path):
    p = _cfg(tmp_path, {"prot": 1})
    r = _orch(str(p), cwd=tmp_path)
    assert r.returncode != 0 and "unknown \"server\" setting(s) prot" in r.stderr


def test_init_writes_a_server_block(tmp_path):
    r = _orch("init", "hq.json", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    cfg = json.loads((tmp_path / "hq.json").read_text())
    assert cfg["server"] == {"host": "127.0.0.1", "port": 8000, "data_dir": ".", "departments": "all"}
    assert cfg["agents"]
