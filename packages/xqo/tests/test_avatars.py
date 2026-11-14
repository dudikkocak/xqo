"""Optional avatar per agent (and for the user node) on the dashboard."""
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from xqo.avatars import resolve_avatar
from xqo.config import build
from xqo.web import create_app

ROOT = Path(__file__).resolve().parent.parent
PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c63f8cfc0f01f0005"
                    "0101ff5d5c9e2e0000000049454e44ae426082")


def test_resolve_avatar(tmp_path):
    (tmp_path / "a.png").write_bytes(PNG)
    assert resolve_avatar(None, tmp_path, "x") is None
    assert resolve_avatar("a.png", tmp_path, "x")["kind"] == "file"
    assert resolve_avatar("https://e.com/a.webp", tmp_path, "x") == {"kind": "url", "url": "https://e.com/a.webp"}
    assert resolve_avatar("🧪", tmp_path, "x") == {"kind": "text", "text": "🧪"}
    assert resolve_avatar("QA", tmp_path, "x") == {"kind": "text", "text": "QA"}
    with pytest.raises(ValueError, match="not found"):
        resolve_avatar("missing.png", tmp_path, "x")
    with pytest.raises(ValueError, match="must be one of"):
        resolve_avatar("evil.html", tmp_path, "x")
    with pytest.raises(ValueError, match="too long"):
        resolve_avatar("a long name", tmp_path, "x")
    (tmp_path / "big.png").write_bytes(b"0" * (2 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="2 MB"):
        resolve_avatar("big.png", tmp_path, "x")


def test_avatars_on_the_dashboard(tmp_path):
    cfg = json.loads((ROOT / "agents.json").read_text())
    (tmp_path / "avatars").mkdir()
    (tmp_path / "avatars" / "me.png").write_bytes(PNG)
    (tmp_path / "avatars" / "bot.png").write_bytes(PNG)
    cfg["user_avatar"] = "avatars/me.png"
    first, last = cfg["agents"][0]["name"], cfg["agents"][-1]["name"]
    cfg["agents"][0]["avatar"] = "avatars/bot.png"
    if first != last:
        cfg["agents"][-1]["avatar"] = "🤖"
    dept_img = None
    p = tmp_path / "agents.json"
    p.write_text(json.dumps(cfg))
    if (ROOT / "departments").is_dir():  # keep the department files next to the config
        shutil.copytree(ROOT / "departments", tmp_path / "departments")
        if first == last:  # hq's agents.json has one agent: the text avatar goes on a department member,
            df = tmp_path / "departments" / "marketing.json"  # and an image relative to the department file
            d = json.loads(df.read_text())
            last = d["agents"][0]["name"]
            d["agents"][0]["avatar"] = "🤖"
            d["agents"][1]["avatar"] = "mk.png"
            (df.parent / "mk.png").write_bytes(PNG)
            df.write_text(json.dumps(d))
            dept_img = d["agents"][1]["name"]
    b = build(p, tmp_path / "a", str(tmp_path / "e.jsonl"), mock=True)
    with TestClient(create_app(b)) as c:
        agents = {a["name"]: a for a in c.get("/api/agents").json()}
        img = agents[first]["avatar"]
        assert img["kind"] == "image" and img["url"].startswith(f"/api/avatars/{first}?v=")
        assert str(tmp_path) not in json.dumps(agents)  # no local paths reach the browser
        assert agents[last]["avatar"] == {"kind": "text", "text": "🤖"}
        assert all(a["avatar"] is None for n, a in agents.items() if n not in (first, last, dept_img))
        if dept_img:
            assert c.get(agents[dept_img]["avatar"]["url"]).content == PNG
        r = c.get(img["url"])
        assert r.status_code == 200 and r.content == PNG and r.headers["content-type"] == "image/png"
        assert r.headers["content-security-policy"] == "sandbox"
        assert c.get("/api/config").json()["user_avatar"]["url"].startswith("/api/avatars/user?v=")
        assert c.get("/api/avatars/user").content == PNG
        assert c.get(f"/api/avatars/{last}").status_code == 404  # text avatars have no file
        assert c.get("/api/avatars/nobody").status_code == 404
    cfg["agents"][0]["avatar"] = "avatars/none.png"
    p.write_text(json.dumps(cfg))
    with pytest.raises(ValueError, match="not found"):
        build(p, tmp_path / "a2", str(tmp_path / "e2.jsonl"), mock=True)


# ---------------------------------------------------------------- the drop-in avatars folder
def _folder_setup(tmp_path, **extra):
    shutil.copytree(ROOT / "departments", tmp_path / "departments")
    cfg = json.loads((ROOT / "agents.json").read_text())
    cfg.update(extra)
    (tmp_path / "agents.json").write_text(json.dumps(cfg))
    return tmp_path / "agents.json", cfg["agents"][0]["name"]


def _bus(tmp_path, p):
    return build(p, tmp_path / "a", str(tmp_path / "l" / "e.jsonl"), mock=True)


def test_folder_images_become_avatars_without_naming_them(tmp_path):
    p, first = _folder_setup(tmp_path)
    (tmp_path / "avatars").mkdir()
    (tmp_path / "avatars" / f"{first}.png").write_bytes(PNG)
    (tmp_path / "avatars" / "user.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    (tmp_path / "avatars" / "stranger.png").write_bytes(PNG)  # no such agent: ignored
    bus = _bus(tmp_path, p)
    c = TestClient(create_app(bus))
    assert bus.agents[first].avatar["kind"] == "file" and bus.user_avatar["type"] == "image/svg+xml"
    r = c.get(f"/api/avatars/{first}")
    assert r.status_code == 200 and r.content == PNG and r.headers["content-type"] == "image/png"
    assert c.get("/api/avatars/user").status_code == 200 and c.get("/api/avatars/stranger").status_code == 404
    assert c.get("/api/config").json()["user_avatar"]["url"].startswith("/api/avatars/user?v=")
    assert any(a["avatar"] is None for a in c.get("/api/agents").json())  # the others have none


def test_explicit_avatar_wins_and_the_folder_can_be_switched_off_or_moved(tmp_path):
    p, first = _folder_setup(tmp_path, avatars_dir="pics")
    (tmp_path / "pics").mkdir()
    (tmp_path / "pics" / f"{first}.png").write_bytes(PNG)
    assert _bus(tmp_path, p).agents[first].avatar["path"].endswith(f"pics/{first}.png")  # a custom folder
    cfg = json.loads(p.read_text())
    cfg["agents"][0]["avatar"] = "🧪"
    p.write_text(json.dumps(cfg))
    assert _bus(tmp_path, p).agents[first].avatar == {"kind": "text", "text": "🧪"}  # explicit wins
    cfg["agents"][0].pop("avatar")
    cfg["avatars_dir"] = False
    p.write_text(json.dumps(cfg))
    assert _bus(tmp_path, p).agents[first].avatar is None  # switched off
    cfg["avatars_dir"] = 5
    p.write_text(json.dumps(cfg))
    with pytest.raises(ValueError, match="avatars_dir"):
        _bus(tmp_path, p)


def test_folder_ignores_oversized_empty_other_types_and_links_out(tmp_path):
    from xqo.avatars import from_folder
    f = tmp_path / "av"
    f.mkdir()
    (tmp_path / "secret.png").write_bytes(PNG)
    (f / "big.png").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    (f / "empty.png").write_bytes(b"")
    (f / "notes.txt").write_text("x")
    (f / "link.png").symlink_to(tmp_path / "secret.png")  # points outside the folder
    for name in ("big", "empty", "notes", "link", "missing", "../secret", "a/b", ""):
        assert from_folder(f, name) is None, name
    assert from_folder(None, "x") is None and from_folder(tmp_path / "nofolder", "x") is None
    (f / "ok.JPG").write_bytes(PNG)
    assert from_folder(f, "ok")["type"] == "image/jpeg"


def test_reload_picks_up_new_images_and_backup_includes_them(tmp_path):
    from xqo.config import reload_departments
    p, first = _folder_setup(tmp_path)
    bus = _bus(tmp_path, p)
    assert bus.agents[first].avatar is None and bus.user_avatar is None
    (tmp_path / "avatars").mkdir()
    (tmp_path / "avatars" / f"{first}.webp").write_bytes(PNG)
    (tmp_path / "avatars" / "user.png").write_bytes(PNG)
    reload_departments(bus)
    assert bus.agents[first].avatar["type"] == "image/webp" and bus.user_avatar["type"] == "image/png"
    from xqo import backup
    assert any(a.endswith(f"avatars/{first}.webp") for a in map(str, backup._avatars(bus)))
