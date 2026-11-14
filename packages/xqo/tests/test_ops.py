"""Operations: backup / restore, data versions and migrations, check, drain, health."""
import asyncio
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from xqo import backup, migrations
from xqo.bus import Draining
from xqo.config import build
from xqo.web import create_app

ROOT = Path(__file__).resolve().parent.parent


def instance(tmp_path: Path) -> Path:
    """An instance folder: agents.json, departments/, skills/ and its data next to them (like Docker's /data)."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "agents.json", tmp_path / "agents.json")
    shutil.copytree(ROOT / "departments", tmp_path / "departments")
    s = tmp_path / "skills" / "notes"
    s.mkdir(parents=True)
    (s / "SKILL.md").write_text("---\nname: notes\ndescription: Notes.\n---\nBe brief.\n")
    return tmp_path


def bus_at(folder: Path, **kw):
    b = build(folder / "agents.json", folder / "artifacts", str(folder / "logs" / "events.jsonl"), mock=True, **kw)
    b.data_dir = folder
    return b


def cli(*args, cwd=None):
    return subprocess.run([sys.executable, "-m", "xqo", *args], capture_output=True, text=True,
                          cwd=cwd or ROOT, timeout=120)


# ------------------------------------------------------------------ backup / restore
def test_backup_scopes_skip_secrets_and_restore(tmp_path):
    src = instance(tmp_path / "src")
    b = bus_at(src)
    t = asyncio.run(b.submit("Build fib"))
    ch = Path(b.artifacts.root) / "office-assets"
    ch.mkdir()
    (ch / "state.json").write_text("{}")
    (ch / "signin.tokens.json").write_text('{"refresh": "secret"}')
    (Path(b.artifacts.root) / t.id / ".venv").mkdir()
    (Path(b.artifacts.root) / t.id / ".venv" / "x").write_text("x")
    (Path(b.artifacts.root) / "schedules.json.tmp").write_text("{}")

    full = backup.create(b, "full", data_dir=src)
    names = set(tarfile.open(full["path"]).getnames())
    assert {"MANIFEST.json", "config/agents.json", "config/departments/engineering.json",
            "config/skills/notes/SKILL.md", "data/artifacts/office-assets/state.json", "data/logs/events.jsonl"} <= names
    assert f"data/artifacts/{t.id}/workspace/src/fib.py" in names
    assert not any("tokens" in n or ".venv" in n or n.endswith(".tmp") or "/backups/" in n for n in names)

    state = set(tarfile.open(backup.create(b, "state", data_dir=src)["path"]).getnames())
    assert "data/artifacts/office-assets/state.json" in state and not any(t.id in n for n in state)
    cfg_only = set(tarfile.open(backup.create(b, "config", data_dir=src)["path"]).getnames())
    assert not any(n.startswith("data/") for n in cfg_only)

    m = backup.verify(full["path"])
    assert m["version"] and m["data_version"] == migrations.DATA_VERSION and m["config"] == "config/agents.json"
    dest = tmp_path / "restored"
    assert backup.restore(full["path"], dest, dry_run=True)["files"] == len(m["files"]) and not dest.exists()
    backup.restore(full["path"], dest)
    assert (dest / "agents.json").is_file() and (dest / "artifacts" / t.id / "workspace" / "src" / "fib.py").is_file()
    b2 = bus_at(dest)
    assert t.id in b2.tasks and [p.name for p in b2.skills.packs.values()] == ["notes"]
    with pytest.raises(ValueError, match="not empty"):
        backup.restore(full["path"], dest)


def test_verify_catches_tampering_and_unsafe_names(tmp_path):
    src = instance(tmp_path / "src")
    b = bus_at(src)
    path = backup.create(b, "config", data_dir=src)["path"]
    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(path) as t_in, tarfile.open(bad, "w:gz") as t_out:
        for m in t_in:
            data = t_in.extractfile(m).read()
            if m.name == "config/agents.json":
                data = data.replace(b"planner", b"plannex")
                m.size = len(data)
            import io
            t_out.addfile(m, io.BytesIO(data))
    with pytest.raises(ValueError, match="checksum"):
        backup.verify(bad)
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(path) as t_in, tarfile.open(evil, "w:gz") as t_out:
        import io
        mf = t_in.extractfile("MANIFEST.json").read()
        info = tarfile.TarInfo("MANIFEST.json")
        info.size = len(mf)
        t_out.addfile(info, io.BytesIO(mf))
        x = tarfile.TarInfo("config/../../escape.txt")
        x.size = 1
        t_out.addfile(x, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="unsafe"):
        backup.verify(evil)


def test_prune_keeps_newest(tmp_path):
    for i in range(4):
        (tmp_path / f"{backup.PREFIX}2026010{i}T000000Z-full.tar.gz").write_text("x")
    (tmp_path / f"{backup.PREFIX}20260101T000000Z-config.tar.gz").write_text("x")
    removed = backup.prune(tmp_path, 2)
    assert removed == [f"{backup.PREFIX}20260100T000000Z-full.tar.gz", f"{backup.PREFIX}20260101T000000Z-full.tar.gz"]
    assert len(list(tmp_path.glob("*"))) == 3


# ------------------------------------------------------------------ migrations
def test_migrations_mark_fresh_and_old_data_and_stop_on_newer(tmp_path, monkeypatch):
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    assert migrations.migrate(fresh) == ["v1: start versioning the data dir"]
    assert migrations.read_data_version(fresh) == migrations.DATA_VERSION and migrations.migrate(fresh) == []

    old = tmp_path / "data" / "artifacts"  # data from before versioning, with runtime settings
    (old / "channels").mkdir(parents=True)
    (old / "schedules.json").write_text('{"v": "old"}')
    (old / "channels" / "x.tokens.json").write_text("secret")
    ran = []

    def to_v2(root):
        ran.append(root)
        (root / "schedules.json").write_text('{"v": "new"}')
    monkeypatch.setattr(migrations, "DATA_VERSION", 2)
    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS + [(2, "new schedules format", to_v2)])
    assert migrations.pending(old) == [(1, "start versioning the data dir"), (2, "new schedules format")]
    assert len(migrations.migrate(old)) == 2 and ran == [old]
    [snap] = (tmp_path / "data" / "backups").glob("pre-migrate-v0-v2-*.tar.gz")
    with tarfile.open(snap) as t:
        assert t.extractfile("artifacts/schedules.json").read() == b'{"v": "old"}'
        assert not any("tokens" in n for n in t.getnames())

    monkeypatch.setattr(migrations, "DATA_VERSION", 1)
    with pytest.raises(migrations.DataTooNew, match="newer"):
        migrations.migrate(old)
    with pytest.raises(ValueError, match="newer"):
        build(ROOT / "agents.json", old, str(tmp_path / "e.jsonl"), mock=True)


# ------------------------------------------------------------------ drain and health
def test_drain_refuses_new_work_and_health_reports(tmp_path):
    folder = instance(tmp_path)
    b = bus_at(folder)
    with TestClient(create_app(b)) as c:
        h = c.get("/api/health").json()
        assert h["ok"] and h["draining"] is False and h["data_version"] == migrations.DATA_VERSION
        assert c.post("/api/admin/drain").json()["draining"] is True
        r = c.post("/api/tasks", json={"prompt": "x"})
        assert r.status_code == 503 and "draining" in r.json()["detail"]
        with pytest.raises(Draining):
            asyncio.run(b.create("x"))
        assert b.on_hold()
        c.post("/api/admin/drain", json={"on": False})
        assert c.post("/api/tasks", json={"prompt": "Build fib"}).status_code == 200

        res = c.post("/api/admin/backup", json={"scope": "config", "keep": 3}).json()
        assert Path(res["path"]).parent == folder / "backups" and res["scope"] == "config"
        assert b.quiesced == 0
        assert any(e["kind"] == "backup" for e in b.history)


# ------------------------------------------------------------------ CLI
def test_cli_check_backup_restore(tmp_path):
    folder = instance(tmp_path / "hq")
    r = cli("--config", str(folder / "agents.json"), "--data-dir", str(folder), "check")
    assert r.returncode == 0 and "check passed" in r.stdout and "1 skill pack(s)" in r.stdout, r.stdout + r.stderr
    r = cli("--config", str(folder / "agents.json"), "--data-dir", str(folder), "backup", "--url",
            "http://127.0.0.1:9", "--scope", "state")
    assert r.returncode == 0 and "writing the archive directly" in r.stdout, r.stdout + r.stderr
    [arc] = (folder / "backups").glob("*.tar.gz")
    assert "ok:" in cli("backup", "verify", str(arc)).stdout
    r = cli("backup", "restore", str(arc), "--to", str(tmp_path / "new"))
    assert r.returncode == 0 and (tmp_path / "new" / "agents.json").is_file(), r.stdout + r.stderr

    cfg = json.loads((folder / "agents.json").read_text())
    cfg["agents"][0]["skill_packs"] = ["ghost"]
    cfg["agents"][0]["tools"] = {"deny": "bad"}
    (folder / "agents.json").write_text(json.dumps(cfg))
    r = cli("--config", str(folder / "agents.json"), "--data-dir", str(folder), "check")
    assert r.returncode == 1 and "check failed" in r.stdout and "tools.deny" in r.stdout
    assert "xqo" in cli("--version").stdout
