"""Idle activities in the office: the schema, the config, the shared director (run with node), and the shipped catalog."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from xqo.plugins.office.view import ACTS, OfficeConfig, validate_layout, _idle

STATIC = Path(__file__).resolve().parent.parent / "xqo" / "plugins" / "office" / "static"


def test_idle_config_defaults_to_on_and_validates():
    c = OfficeConfig.from_dict({})
    assert c.idle == {"enabled": True, "activities": list(ACTS), "chat": True, "max_away": 0.6}
    assert _idle(False)["enabled"] is False and _idle(True)["enabled"] is True
    assert _idle({"activities": ["read", "sit"], "chat": False, "max_away": 0.3}) == \
        {"enabled": True, "activities": ["read", "sit"], "chat": False, "max_away": 0.3}
    for bad in ({"activities": ["fly"]}, {"activities": ["read", "read"]}, {"activities": "read"}, {"chat": "yes"},
                {"max_away": 2}, {"max_away": True}, {"enabled": 1}, {"colour": 1}, "on", 3):
        with pytest.raises(ValueError, match="office.idle"):
            _idle(bad)


def test_catalog_anchor_act_is_validated():
    ok = {"catalog": {"props": {"shelf2": {"label": "Shelf", "builtin": "bookshelf", "size": [2, 1],
                                            "anchors": [{"type": "social", "act": "read", "ox": 1, "oy": 1.5}]}}}}
    validate_layout(ok)
    ok["catalog"]["props"]["shelf2"]["anchors"][0]["act"] = "juggle"
    with pytest.raises(ValueError, match="act must be one of"):
        validate_layout(ok)


def test_the_shipped_catalog_has_the_idle_spots():
    props = json.loads((STATIC / "office3d" / "catalog.json").read_text())["props"]
    acts = {}
    for k, v in props.items():
        for a in v.get("anchors", []):
            if a.get("act"):
                acts.setdefault(a["act"], []).append(k)
    assert set(acts) == set(ACTS)
    assert "bookshelf_packed" in acts["read"] and "tall_potted_plant" in acts["tend"]
    assert "wooden_framed_whiteboard" in acts["write"] and "sofa" in acts["sit"] and "coffee_bar_counter" in acts["drink"]
    assert len(props["sofa"]["anchors"]) == 4 and props["sofa"]["size"] == [5, 1.2]  # four seats


JS = r"""
import {IdleDirector, ACTS, CHAT} from "%s/officeidle.js";
const seeded = s => () => (s = (s * 1664525 + 1013904223) %% 4294967296) / 4294967296;
const spots = [
  {name: "shelf#0", act: "read", prop: "shelf"}, {name: "shelf#1", act: "read", prop: "shelf"},
  {name: "sofa#0", act: "sit", prop: "sofa"}, {name: "sofa#1", act: "sit", prop: "sofa"}, {name: "sofa#2", act: "sit", prop: "sofa"},
  {name: "board#0", act: "write", prop: "board"}, {name: "plant#0", act: "tend", prop: "plant"},
  {name: "bar#0", act: "drink", prop: "bar"}, {name: "plain", act: null, prop: "x"},
];
const out = {};
// 1. every act is reachable, a spot is never shared, nothing for a plain spot
{
  const seen = new Set(), clash = [];
  for (let seed = 1; seed < 200; seed++) {
    const d = new IdleDirector(spots, {chat: false}, seeded(seed)), used = new Set();
    for (const id of ["a", "b", "c", "d"]) {
      const p = d.plan(id, {idle: [], total: 10}, 0);
      if (!p) continue;
      seen.add(p.act); if (used.has(p.spot.name)) clash.push(p.spot.name); used.add(p.spot.name);
      if (p.spot.name === "plain") clash.push("plain");
    }
  }
  out.acts = [...seen].sort(); out.clash = clash;
}
// 2. chat: a group shares an end time, distinct spots of one prop, and everyone is in the plan
{
  let groups = 0, bad = 0;
  for (let seed = 1; seed < 200; seed++) {
    const d = new IdleDirector(spots, {}, seeded(seed));
    const p = d.plan("a", {idle: ["a", "b", "c"], total: 10}, 0);
    if (p && p.act === "chat") {
      groups++;
      const crew = p.crew.map(m => d.plans.get(m));
      const props = new Set(crew.map(c => c.spot.prop)), names = new Set(crew.map(c => c.spot.name));
      if (props.size !== 1 || names.size !== crew.length || new Set(crew.map(c => c.until)).size !== 1 || crew.length < 2 || crew.length > CHAT.groupMax) bad++;
    }
  }
  out.groups = groups > 10; out.badGroups = bad;
}
// 3. plans expire, release frees the spot, repeats are rare, max_away holds, disabled does nothing
{
  const d = new IdleDirector(spots, {chat: false}, seeded(7));
  const p = d.plan("a", {total: 10}, 1000);
  out.same = d.plan("a", {total: 10}, 1500) === p;                   // still on it
  out.walking = d.current("a", 1000 + 100000) === p;                   // 100 s on the way: still on it (the stay starts at arrival)
  d.arrive("a", 5000); out.stays = p.until === 5000 + p.dur && d.current("a", 5000 + p.dur - 1) === p;
  out.expired = d.current("a", p.until + 1) === null && d.away() === 0;
  const lost = new IdleDirector(spots, {chat: false}, seeded(9)), lp = lost.plan("z", {total: 10}, 0);
  out.gaveUp = lost.current("z", 121000) === null && lp !== null;     // never arrived (unreachable): dropped after WALK_MAX
  const q = d.plan("b", {total: 10}, 0); d.release("b"); out.freed = !d.taken(q.spot.name);
  const few = new IdleDirector(spots, {chat: false, max_away: 0.2}, seeded(3));
  let away = 0; for (const id of "abcdefghij") if (few.plan(id, {total: 10}, 0)) away++;
  out.cap = away;                                                    // floor(10 * 0.2) = 2
  out.off = new IdleDirector(spots, {enabled: false}).plan("a", {total: 10}, 0) === null;
  out.limited = new IdleDirector(spots, {activities: ["tend"]}, seeded(1)).spots.every(s => s.act === "tend");
  out.empty = new IdleDirector([], {}).plan("a", {total: 10}, 0) === null;
  let rep = 0, n = 0; const e = new IdleDirector(spots, {chat: false}, seeded(11));
  let last = null; for (let i = 0; i < 400; i++) {e.release("a"); const x = e.plan("a", {total: 10}, 0); if (x) {n++; if (x.act === last) rep++; last = x.act}}
  out.repeatRate = rep / n < 0.2;
  out.free = new IdleDirector(spots, {chat: false}, seeded(2)).plan("a", {total: 10, free: s => s.act === "tend"}, 0).act;
}
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_director_behaviour(tmp_path):
    f = tmp_path / "t.mjs"
    f.write_text(JS % STATIC)
    r = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    o = json.loads(r.stdout)
    assert o["acts"] == sorted(ACTS) and o["clash"] == []
    assert o["groups"] and o["badGroups"] == 0
    assert o["walking"] and o["stays"] and o["gaveUp"]
    assert o["same"] and o["expired"] and o["freed"] and o["cap"] == 2 and o["off"] and o["limited"] and o["empty"] and o["repeatRate"]
    assert o["free"] == "tend"


# ---------------------------------------------------------------- in a real browser
import glob
import socket
import threading
import time

from fastapi.testclient import TestClient  # noqa: F401  (kept for parity with the other browser tests)

from xqo.config import build
from xqo.web import create_app

ROOT = STATIC.parent.parent.parent.parent


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Office:
    def __init__(self, tmp_path, office=None):
        import uvicorn
        shutil.copytree(ROOT / "departments", tmp_path / "departments")
        cfg = json.loads((ROOT / "agents.json").read_text())
        if office is not None:
            cfg["office"] = office
        (tmp_path / "agents.json").write_text(json.dumps(cfg))
        app = create_app(build(tmp_path / "agents.json", tmp_path / "a", str(tmp_path / "l" / "e.jsonl"), mock=True))
        self.port = _free_port()
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                break
            time.sleep(0.05)
        self.url = f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.server.should_exit = True
        self.thread.join(5)


@pytest.fixture
def office_page(tmp_path):
    pw = pytest.importorskip("playwright.sync_api")
    made = []

    def make(view, office=None):
        srv = _Office(tmp_path / f"o{len(made)}", office) if (tmp_path / f"o{len(made)}").mkdir() is None else None
        made.append(srv)
        ctx = pw.sync_playwright().start()
        made.append(ctx)
        browser = None
        for exe in (None, *sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome"))):
            try:
                browser = ctx.chromium.launch(executable_path=exe)
                break
            except Exception:
                continue
        if browser is None:
            pytest.skip("no chromium available for playwright")
        made.append(browser)
        pg = browser.new_page(viewport={"width": 1400, "height": 900})
        pg.goto(f"{srv.url}/office?view={view}")
        pg.wait_for_function("window.office && office.r && office.r.idle", timeout=20000)
        return pg, srv

    yield make
    for x in reversed(made):
        try:
            x.stop() if isinstance(x, _Office) else (x.close() if hasattr(x, "close") else x.stop())
        except Exception:
            pass


ARRIVED = {
    "2d": """(() => { const d = office.r.idle, mv = office.r.mv; return [...d.plans].filter(([id, p]) => { const t = mv.getCitizen(id).getTilePosition();
        return t.x === p.spot.x && t.y === p.spot.y && p.at }).map(([id, p]) => [id, p.act, p.base || '', p.spot.name]) })()""",
    "3d": """(() => { const d = office.r.idle, A = office.r.agents; return [...d.plans].filter(([id, p]) => { const a = A.get(id);
        return p.at && !a.path.length && a.goal && a.goal.c === p.spot.tile.c && a.goal.r === p.spot.tile.r }).map(([id, p]) => [id, p.act, p.base || '', p.spot.name]) })()""",
}


@pytest.mark.parametrize("view", ["2d", "3d"])
def test_idle_agents_walk_to_spots_pretend_and_return_to_work(office_page, view):
    pg, srv = office_page(view)
    if view == "3d":
        try:
            pg.wait_for_function("office.r.idle && office.r.idle.spots.length", timeout=15000)
        except Exception:
            pytest.skip("WebGL is not available in this browser")
    pg.wait_for_function(f"({ARRIVED[view]}).length >= 1", timeout=60000)  # somebody got there and started pretending
    plans = pg.evaluate("[...office.r.idle.plans].map(([id, p]) => [id, p.spot.name])")
    assert len({n for _, n in plans}) == len(plans)  # a spot is never shared
    assert pg.evaluate("office.r.idle.spots.length") >= 10 and pg.evaluate("office.r.idle.spots.every(s => s.act)")
    # the agent gets work (the planner asks you something, so it stays busy): it leaves its spot at once
    busy = pg.evaluate("[...office.r.idle.plans.keys()][0]")
    import urllib.request
    req = urllib.request.Request(f"{srv.url}/api/tasks", data=json.dumps({"prompt": "please confirm", "agent": busy}).encode(),
                                 headers={"content-type": "application/json"})
    urllib.request.urlopen(req, timeout=10).read()
    pg.wait_for_function(f"!office.r.idle.plans.has({json.dumps(busy)})", timeout=15000)
    for _ in range(4):  # and it does not start pretending again while it has work
        time.sleep(1)
        assert not pg.evaluate(f"office.r.idle.plans.has({json.dumps(busy)})")


@pytest.mark.parametrize("view", ["2d", "3d"])
def test_idle_can_be_turned_off(office_page, view):
    pg, srv = office_page(view, {"idle": False})
    assert pg.evaluate("office.r.idle.enabled") is False
    time.sleep(6)
    assert pg.evaluate("office.r.idle.plans.size") == 0


def test_sofa_seats_are_inside_the_cushion_and_apart(office_page):
    pg, srv = office_page("3d")
    try:
        pg.wait_for_function("office.r.idle && office.r.idle.spots.length", timeout=15000)
    except Exception:
        pytest.skip("WebGL is not available in this browser")
    seats = pg.evaluate("office.r.idle.spots.filter(s => s.act === 'sit').map(s => [s.fx, s.fy, s.seat.x, s.seat.z])")
    assert len(seats) == 4
    fx = seats[0][0]
    xs = sorted(s[2] for s in seats)
    assert all(fx - 2.5 + 0.25 + 0.3 <= x <= fx + 2.5 - 0.25 - 0.3 for x in xs)  # between the arms, with room for a body
    assert all(b - a >= 1.0 for a, b in zip(xs, xs[1:]))  # no two seats overlap
    assert all(abs(s[3] - seats[0][3]) < 1e-6 for s in seats)  # one row


def test_idle_sprite_sheets_exist_and_idle_anims_are_registered():
    Image = pytest.importorskip("PIL.Image")
    for n in ("nova", "rio", "dexter", "morty"):
        for k in ("idle", "stand"):
            im = Image.open(STATIC / "office/citizens" / f"{n}_{k}.png")
            assert im.size == (256, 256) and im.getbbox()
    js = (STATIC / "office2d.js").read_text()
    idle = (STATIC / "officeidle.js").read_text()
    import re
    for anim in re.findall(r'anim: "(\w+)"', idle):
        assert f"{anim}:" in js or f"{anim}," in js, anim  # every act's animation is registered for the 2D sprites


def test_2d_idle_agents_play_the_act_animation(office_page):
    pg, srv = office_page("2d")
    pg.wait_for_function(f"({ARRIVED['2d']}).length >= 1", timeout=60000)
    got = pg.evaluate("""(() => [...office.r.idle.plans].filter(([id, p]) => p.at).map(([id, p]) =>
        [p.act === 'chat' ? p.base : p.act, office.r.mv.getCitizen(id).animator.getCurrentAnimation()]))()""")
    want = {"read": "read", "drink": "drink", "sit": "sit_down", "write": "write", "tend": "tend"}
    assert got and all(a in (want[k], "talking", "idle_left", "idle_right") for k, a in got), got


def test_2d_facing_uses_side_sprites_at_a_spot(office_page):
    pg, srv = office_page("2d")
    f = lambda act, s, **k: pg.evaluate("([a, s, k]) => office.r.idleAnim({act: a, base: a, spot: s, crew: []}, 0)", [act, s, k])
    spot = {"x": 10, "y": 10, "rot": 0}
    assert f("read", {**spot, "fx": 13, "fy": 10.5}) == "idle_right"  # the prop is to the right
    assert f("drink", {**spot, "fx": 7, "fy": 10.5}) == "idle_left"
    assert f("read", {**spot, "fx": 10.5, "fy": 8}) == "read"  # above: the act's own sprite
    assert f("write", {**spot, "fx": 10.5, "fy": 8}) == "write"
    assert f("sit", {**spot, "fx": 10.5, "fy": 8}) == "sit_down" and f("sit", {**spot, "fx": 10.5, "fy": 8, "rot": 180}) == "sit_up"


def test_office_edit_can_be_switched_on_in_the_config(tmp_path):
    from xqo.plugins.office.view import OfficeConfig
    assert OfficeConfig.from_dict(None).edit is False
    assert OfficeConfig.from_dict({"edit": True}).edit is True
    with pytest.raises(ValueError, match="office.edit"):
        OfficeConfig.from_dict({"edit": "yes"})
    shutil.copytree(ROOT / "departments", tmp_path / "departments")
    cfg = json.loads((ROOT / "agents.json").read_text())
    cfg["office"] = {"edit": True}
    (tmp_path / "agents.json").write_text(json.dumps(cfg))
    bus = build(tmp_path / "agents.json", tmp_path / "a", str(tmp_path / "l" / "e.jsonl"), mock=True)
    assert bus.office.edit is True  # serve turns editing on from this (or from --edit)
