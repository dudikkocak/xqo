"""Office view: config, the planners list, the relay to other HQs, and the page in a real browser when one is available."""
import glob
import json
import socket
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from xqo.config import build
from xqo.plugins.office.view import LayoutStore, OfficeConfig, planners, relay_target, validate_layout
from xqo.web import create_app

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "xqo" / "plugins" / "office" / "static"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _config(tmp_path, office=None, name="agents.json"):
    cfg = json.loads((ROOT / "agents.json").read_text())
    if office is not None:
        cfg["office"] = office
    p = tmp_path / name
    p.write_text(json.dumps(cfg))
    return p


def _bus(tmp_path, office=None, tag="a", edit=True):
    bus = build(_config(tmp_path, office, f"{tag}.json"), tmp_path / tag / "art", str(tmp_path / tag / "e.jsonl"),
                mock=True, departments_dir=ROOT / "departments")
    bus.office_edit = edit  # `serve --edit`
    return bus


class _Server:
    def __init__(self, app):
        import uvicorn
        self.port = _free_port()
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                break
            time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(5)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"


def test_config_parsing(monkeypatch):
    c = OfficeConfig.from_dict({"name": "hq-a", "planners": ["planner"], "hqs": [{"name": "hq-b", "url": "http://10.0.0.2:8002/"},
                               {"name": "mkt", "url": "http://marketing:8000", "link": "http://127.0.0.1:8002"}]})
    assert (c.name, c.planners, c.hqs[0].url, c.hqs[0].link) == ("hq-a", ["planner"], "http://10.0.0.2:8002", "http://10.0.0.2:8002")
    assert c.hqs[1].link == "http://127.0.0.1:8002"
    for bad in ({"hqs": [{"name": "b", "url": "file:///etc"}]}, {"hqs": [{"name": "b", "url": "http://u:p@host"}]},
                {"hqs": [{"name": "b", "url": "http://x"}, {"name": "b", "url": "http://y"}]},
                {"hqs": [{"name": "../b", "url": "http://x"}]}, {"hqs": [{"name": "b", "url": "http://x", "token": "t"}]},
                {"hqs": [{"name": "b", "url": "http://x", "link": "javascript:alert(1)"}]}, {"planners": "planner"}, {"colour": 1}):
        with pytest.raises(ValueError):
            OfficeConfig.from_dict(bad)
    monkeypatch.setenv("XQO_OFFICE_HQS", "hq-c=http://127.0.0.1:8003;hq-d=https://d.example")
    assert [h.name for h in OfficeConfig.from_dict({"hqs": [{"name": "x", "url": "http://x"}]}).hqs] == ["hq-c", "hq-d"]


def test_layout_format():
    doc = {"version": 1, "theme": {"floor": "#aabbcc"}, "rooms": {"engineering": {"size": [14, 9]}},
           "props": {"desk:planner": {"x": 3, "y": 2.5, "rot": 90}, "plant:hq": {"hide": True}},
           "added": [{"uid": "u-1", "type": "sofa", "zone": "@lounge", "x": 4, "y": 1}],
           "agents": {"planner": {"character": "character-male-c", "desk": "u-1"}},
           "catalog": {"props": {"custom-robot": {"label": "Robot", "glb": "assets/robot.glb", "size": [1, 1], "blocks": True}},
                       "characters": {"custom-cat": {"glb": "assets/cat.glb"}}}}
    out = validate_layout(doc)
    assert out["added"][0] == {"uid": "u-1", "type": "sofa", "zone": "@lounge", "x": 4, "y": 1, "rot": 0}
    assert out["props"]["desk:planner"] == {"x": 3, "y": 2.5, "rot": 90} and out["catalog"]["props"]["custom-robot"]["size"] == [1, 1]
    assert validate_layout(None) == {"version": 1}
    for bad in ({"version": 2}, {"colours": {}}, {"theme": {"floor": "red"}}, {"props": {"a": {"rot": 45}}},
                {"props": {"a": {"x": float("nan")}}}, {"props": {"a": {"x": 1e9}}}, {"props": {"a": {"hide": "yes"}}},
                {"added": [{"uid": "u", "type": "sofa"}, {"uid": "u", "type": "sofa"}]}, {"added": [{"uid": "u", "type": "../x"}]},
                {"rooms": {"x": {"size": [2, 2]}}}, {"agents": {"a": {"hat": "x"}}},
                {"catalog": {"props": {"x": {"glb": "../../etc/passwd.glb"}}}}, {"catalog": {"props": {"x": {"glb": "https://evil/x.glb"}}}},
                {"catalog": {"props": {"x": {"size": [1, 1]}}}}, {"catalog": {"props": {"x": {"builtin": "sofa", "glb": "a.glb"}}}},
                {"catalog": {"props": {"x": {"builtin": "sofa", "anchors": [{"type": "dance"}]}}}}, {"props": {"\n": {}}}):
        with pytest.raises(ValueError):
            validate_layout(bad)


def test_layout_is_saved_per_hq_and_checked(tmp_path):
    bus = _bus(tmp_path)
    client = TestClient(create_app(bus))
    assert client.get("/api/office/layout").json() == {"layout": {"version": 1}, "rev": "", "assets": []}
    doc = {"props": {"desk:planner": {"x": 4, "y": 3}}, "added": [{"uid": "u-1", "type": "sofa", "zone": "engineering", "x": 2, "y": 6}]}
    assert client.put("/api/office/layout?check=true", json=doc).json()["layout"]["added"][0]["rot"] == 0
    assert not (Path(bus.artifacts.root) / "office.json").exists()  # a check saves nothing
    r = client.put("/api/office/layout?rev=", json=doc)
    assert r.status_code == 200 and r.json()["rev"]
    rev = r.json()["rev"]
    saved = json.loads((Path(bus.artifacts.root) / "office.json").read_text())
    assert saved["props"] == {"desk:planner": {"x": 4, "y": 3}} and client.get("/api/office").json()["layout_rev"] == rev
    assert client.get("/api/office/layout").json()["layout"] == saved
    assert client.put("/api/office/layout?rev=old", json={}).status_code == 409  # someone saved meanwhile
    bad = client.put("/api/office/layout", json={"props": {"x": {"rot": 45}}})
    assert bad.status_code == 400 and "rot" in bad.json()["detail"]
    assert client.put("/api/office/layout", content=b"{not json").status_code == 400
    assert client.put("/api/office/layout", content=b" " * 1_000_001).status_code == 413
    (Path(bus.artifacts.root) / "office.json").write_text('{"props": {"x": {"rot": 7}}}')  # a hand-edited mistake
    got = client.get("/api/office/layout").json()["layout"]
    assert got["version"] == 1 and "ignored" in got["error"] and "props" not in got


def test_custom_models_are_kept_with_the_hq(tmp_path):
    import base64
    bus = _bus(tmp_path)
    client = TestClient(create_app(bus))
    glb = (STATIC / "office3d" / "furniture" / "pottedPlant.glb").read_bytes()
    r = client.post("/api/office/assets", json={"name": "my-plant.glb", "data": base64.b64encode(glb).decode()})
    assert r.status_code == 200 and r.json() == {"name": "my-plant.glb", "size": len(glb)}
    assert (Path(bus.artifacts.root) / "office-assets" / "my-plant.glb").read_bytes() == glb
    got = client.get("/api/office/assets/my-plant.glb")
    assert got.content == glb and got.headers["content-type"] == "model/gltf-binary" and got.headers["x-content-type-options"] == "nosniff"
    assert client.get("/api/office/layout").json()["assets"] == [{"name": "my-plant.glb", "size": len(glb)}]
    for name, data in (("x.glb", b"<html>"), ("../x.glb", glb), ("x.html", glb), (".hidden.glb", glb)):
        assert client.post("/api/office/assets", json={"name": name, "data": base64.b64encode(data).decode()}).status_code == 400, name
    assert client.get("/api/office/assets/..%2Fevents.jsonl").status_code == 404
    assert client.delete("/api/office/assets/my-plant.glb").json() == {"deleted": "my-plant.glb"}
    assert client.get("/api/office/assets/my-plant.glb").status_code == 404 and client.get("/api/office/assets").json() == []
    store = LayoutStore(tmp_path / "s")
    with pytest.raises(ValueError):
        store.save_asset("big.glb", b"glTF" + b"0" * 20_000_001)


def test_relay_only_forwards_what_the_office_uses():
    c = OfficeConfig.from_dict({"hqs": [{"name": "b", "url": "http://127.0.0.1:9"}]})
    assert relay_target(c, "b", "tasks/task_1/chat", "POST") == "http://127.0.0.1:9/api/tasks/task_1/chat"
    assert relay_target(c, "b", "attention", "GET").endswith("/api/attention")
    assert relay_target(c, "b", "office/layout", "GET").endswith("/api/office/layout")
    assert relay_target(c, "b", "office/assets/robot.glb", "GET").endswith("/api/office/assets/robot.glb")
    for path, method in (("office/layout", "POST"), ("office/assets", "POST"), ("office/assets/../x", "GET"), ("limits", "POST"), ("config", "GET"), ("tasks/x/cancel", "POST"), ("trash", "GET"),
                         ("tasks/../limits", "POST"), ("peers/send", "POST"), ("agents", "DELETE")):
        with pytest.raises(PermissionError):
            relay_target(c, "b", path, method)
    with pytest.raises(KeyError):
        relay_target(c, "nope", "agents", "GET")


def test_planners_and_office_endpoint(tmp_path):
    bus = _bus(tmp_path)
    assert planners(bus) == ["planner"]
    client = TestClient(create_app(bus))
    assert client.get("/api/office").json() == {"name": "hq", "title": "xqo", "hq_name": "", "planners": ["planner"], "nicknames": {}, "avatars": {}, "hqs": [], "editable": True, "layout_rev": "", "views": ["3d", "2d"], "idle": {"enabled": True, "activities": ["read", "drink", "sit", "write", "tend"], "chat": True, "max_away": 0.6}}
    assert "office.js" in client.get("/office").text
    assert client.get("/api/config").json()["office"]["name"] == "hq"
    cat = client.get("/plugins/office/office3d/catalog.json").json()  # every catalog model, picture and character is served
    for pid, e in cat["props"].items():
        if "glb" in e:
            assert client.get(f"/plugins/office/office3d/{e['glb']}").status_code == 200, pid
        if "sprite2d" in e:
            assert client.get(f"/plugins/office/office/props/{e['sprite2d']}.png").status_code == 200, pid
        for w in e.get("with", []):
            assert w["type"] in cat["props"]
    for c in cat["characters"].values():
        assert client.get(f"/plugins/office/office3d/{c['glb']}").status_code == 200
    validate_layout({"catalog": {"props": {k: {x: y for x, y in v.items() if x != "with"} for k, v in cat["props"].items()},
                                 "characters": cat["characters"]}})  # the shipped catalog follows the format too
    assert client.get("/plugins/office/vendor/miniverse/miniverse.js").headers["content-type"].startswith(("application/javascript", "text/javascript"))
    named = _bus(tmp_path, {"name": "hq-a", "planners": ["eng_lead", "ghost"]}, "b")
    assert TestClient(create_app(named)).get("/api/office").json()["planners"] == ["eng_lead"]


def test_relay_reads_and_acts_on_another_hq(tmp_path):
    remote = _bus(tmp_path, {"name": "hq-b"}, "remote")
    with _Server(create_app(remote)) as srv:
        local = _bus(tmp_path, {"name": "hq-a", "hqs": [{"name": "hq-b", "url": srv.url}]}, "local")
        client = TestClient(create_app(local))
        assert client.get("/api/office").json()["hqs"] == [{"name": "hq-b", "link": srv.url}]
        assert client.get("/api/office/hq/hq-b/office").json()["name"] == "hq-b"
        assert {a["name"] for a in client.get("/api/office/hq/hq-b/agents").json()} == set(remote.agents)
        assert client.get("/api/office/hq/hq-b/attention", params={"pending": "true"}).json() == []
        r = client.post("/api/office/hq/hq-b/tasks", json={"prompt": "Plan the joint launch", "agent": "planner"})
        assert r.status_code == 200 and r.json()["id"] in remote.tasks and r.json()["id"] not in local.tasks
        assert client.post("/api/office/hq/hq-b/tasks", json={"prompt": "x", "agent": "nobody"}).status_code == 404  # remote's answer
        assert client.post("/api/office/hq/hq-b/limits", json={"enabled": True}).status_code == 403
        assert client.get("/api/office/hq/hq-b/config").status_code == 403
        assert client.get("/api/office/hq/hq-x/agents").status_code == 404
        LayoutStore(remote.artifacts.root).save({"props": {"plant:hq": {"hide": True}}})  # its edited office is shown as it is
        assert client.get("/api/office/hq/hq-b/office/layout").json()["layout"]["props"] == {"plant:hq": {"hide": True}}
        assert client.put("/api/office/hq/hq-b/office/layout", json={}).status_code == 405  # but not edited from here


def test_events_relay_reports_an_unreachable_hq(tmp_path):
    local = _bus(tmp_path, {"hqs": [{"name": "gone", "url": f"http://127.0.0.1:{_free_port()}"}]})
    client = TestClient(create_app(local))
    assert client.get("/api/office/hq/gone/agents").status_code == 502
    with client.stream("GET", "/api/office/hq/gone/events") as r:
        text = "".join(r.iter_text())
    assert "event: relay" in text and '"up": false' in text


@pytest.fixture
def browser(tmp_path):
    pw = pytest.importorskip("playwright.sync_api")
    with _Server(create_app(_bus(tmp_path))) as srv, pw.sync_playwright() as p:
        browser = None
        for exe in (None, *sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome"))):
            try:
                browser = p.chromium.launch(executable_path=exe)
                break
            except Exception:
                continue
        if browser is None:
            pytest.skip("no chromium available for playwright")
        yield srv, browser
        browser.close()


def _open(browser, query="", init=None):
    srv, b = browser
    pg = b.new_page(viewport={"width": 1400, "height": 850})
    if init:
        pg.add_init_script(init)
    pg.goto(f"{srv.url}/office{query}")
    pg.wait_for_function("window.office && window.office.r && window.office.cit.size > 0", timeout=20000)
    return pg


@pytest.mark.parametrize("view", ["3d", "2d"])
def test_office_page_click_an_agent_and_give_it_a_task(browser, view):
    pg = _open(browser, f"?view={view}")
    if view == "3d" and pg.evaluate("window.office.r.kind") != "3d":
        pytest.skip("no WebGL in this browser: " + str(pg.evaluate("window.office.fallback")))
    assert pg.evaluate("window.office.r.kind") == view
    zones = pg.evaluate("window.office.zones.map(z => [z.label, z.members.map(m => m.agent)])")
    assert zones[0] == ["Headquarters", ["planner"]] and {z[0] for z in zones} >= {"engineering", "marketing", "research"}
    homes = pg.evaluate("[...window.office.cit.values()].map(c => c.home)")
    assert all(homes) and len(set(homes)) == len(homes)        # every agent has its own desk
    if view == "3d":  # every agent stands at its own desk, in the 3D scene too
        seats = pg.evaluate("[...window.office.r.agents.values()].map(a => a.seat.c + ',' + a.seat.r)")
        assert len(set(seats)) == len(seats)
    for _ in range(5):  # agents walk around when idle: aim again if the click missed
        xy = pg.evaluate("window.office.r.screenPos('planner')")
        pg.mouse.click(xy["x"], xy["y"])
        try:
            pg.wait_for_selector("#panel h3", timeout=2000)
            break
        except Exception:
            continue
    assert pg.text_content("#panel h3").startswith("planner")
    pg.fill("#newtext", "Write a haiku about offices")
    pg.click("#newsend")
    pg.wait_for_function("document.querySelector('#panel .task') !== null", timeout=10000)
    tasks = pg.evaluate("fetch('/api/tasks').then(r => r.json())")
    assert any(t["assignee"] == "planner" and "haiku" in t["prompt"] for t in tasks)
    assert pg.evaluate("document.querySelector('#taskf').options.length") >= 1


def test_office_falls_back_to_2d_without_webgl(browser):
    pg = _open(browser, "?view=3d", init="const g = HTMLCanvasElement.prototype.getContext; HTMLCanvasElement.prototype.getContext = function (t, ...a) {return /webgl/.test(t) ? null : g.call(this, t, ...a)}")
    assert pg.evaluate("window.office.r.kind") == "2d" and pg.evaluate("window.office.fallback")
    assert pg.evaluate("document.querySelector('#views [data-view=\"2d\"]').classList.contains('on')")


def test_office_3d_assets_are_bundled():
    # nothing is loaded from the internet: three.js and the Kenney models ship with the dashboard
    for f in ["vendor/three/three.module.js", "vendor/three/three.core.js", "vendor/three/addons/loaders/GLTFLoader.js",
              "vendor/three/addons/controls/OrbitControls.js", "vendor/three/addons/utils/SkeletonUtils.js",
              "office3d/characters/Textures/colormap.png", "office3d/furniture/desk.glb"]:
        assert (STATIC / f).is_file(), f
    js = (STATIC / "office3d.js").read_text() + (STATIC / "office.html").read_text()
    assert "http://" not in js.replace("http://www.w3.org", "") and "https://" not in js


def _drag(pg, uid, dx, dy):
    xy = pg.evaluate(f"window.office.r.propScreenPos({uid!r})")
    pg.mouse.move(xy["x"], xy["y"])
    pg.mouse.down()
    pg.mouse.move(xy["x"] + dx, xy["y"] + dy, steps=8)
    pg.mouse.up()
    pg.wait_for_timeout(300)


def test_office_edit_mode_moves_adds_and_saves(browser):
    srv, _ = browser
    pg = _open(browser, "?view=3d")
    if pg.evaluate("window.office.r.kind") != "3d":
        pytest.skip("no WebGL in this browser")
    # the layout math: a turned desk keeps its seat outside its footprint, its chair on that seat
    turned = pg.evaluate("""import('/plugins/office/officelayout.js').then(m => [0, 90, 180, 270].map(rot => {
        const L = m.layout([{key: 'd', label: 'd', members: [{id: 'a'}]}], {props: {'desk:a': {x: 2, y: 2, rot}}}, window.office.cat);
        const d = L.byUid.get('desk:a'), c = L.byUid.get('desk:a/0'), s = d.anchors[0];
        const tx = Math.round(d.x + s.ox), ty = Math.round(d.y + s.oy);
        return {inside: tx >= d.x && tx < d.x + d.w && ty >= d.y && ty < d.y + d.h, chair: [Math.floor(c.x), Math.floor(c.y)], seat: [tx, ty], w: d.w}}))""")
    assert not any(t["inside"] for t in turned) and all(t["chair"] == t["seat"] for t in turned)
    assert [t["w"] for t in turned] == [3, 2, 3, 2]
    pg.click("#editgo")
    pg.wait_for_function("window.office.edit && document.querySelector('#panel h3')")
    assert not any(pg.evaluate("[...window.office.r.agents.values()].map(a => a.grp.visible)"))  # the agents step out
    before = pg.evaluate("window.office.L.byUid.get('desk:planner')")
    _drag(pg, "desk:planner", 0, 60)  # drag the planner's desk down a few tiles
    after = pg.evaluate("window.office.L.byUid.get('desk:planner')")
    assert after["y"] > before["y"] and pg.evaluate("window.office.edit.sel") == "desk:planner"
    pg.keyboard.press("r")
    pg.wait_for_function("window.office.L.byUid.get('desk:planner').rot === 90")
    pg.click("[data-add=sofa]")
    pg.wait_for_function("(window.office.edit.doc.added || []).some(a => a.type === 'sofa')")
    pg.keyboard.press("Control+z")
    pg.wait_for_function("!(window.office.edit.doc.added || []).length")
    pg.keyboard.press("Control+y")
    pg.wait_for_function("(window.office.edit.doc.added || []).length === 1")
    pg.select_option("[data-char=planner]", "character-male-c")
    pg.wait_for_function("window.office.L.characters.planner === 'character-male-c'")
    # an uploaded model: into the HQ's office-assets/, the catalog, and the middle of the view
    pg.set_input_files("#mfile", str(STATIC / "office3d" / "furniture" / "pottedPlant.glb"))
    pg.fill("#mlabel", "Big plant")
    pg.click("#mup")
    pg.wait_for_function("(window.office.edit.doc.added || []).some(a => a.type === 'custom-pottedPlant')", timeout=10000)
    assert pg.evaluate("window.office.cat.props['custom-pottedPlant'].glb") == "assets/pottedPlant.glb"
    pg.click("[data-a=save]")
    pg.wait_for_function("!window.office.edit.dirty")
    saved = pg.evaluate("fetch('/api/office/layout').then(r => r.json())")["layout"]
    assert saved["props"]["desk:planner"]["rot"] == 90 and saved["agents"]["planner"] == {"character": "character-male-c"}
    assert {a["type"] for a in saved["added"]} == {"sofa", "custom-pottedPlant"}
    pg.click("[data-a=done]")
    pg.wait_for_function("!window.office.edit && window.office.r && window.office.r.agents && window.office.r.agents.get('planner') && window.office.r.agents.get('planner').grp.visible")
    seat = pg.evaluate("window.office.r.agents.get('planner').seat")
    desk = pg.evaluate("window.office.L.byUid.get('desk:planner')")
    spot = desk["anchors"][0]
    # the planner sits at its moved desk (on its spot, or next to it when something else stands there)
    assert seat["rot"] == 90 and abs(seat["c"] - round(desk["x"] + spot["ox"])) <= 1 and abs(seat["r"] - round(desk["y"] + spot["oy"])) <= 1
    # the same office in 2D (the sofa and the uploaded plant have no 2D picture: drawn as boxes) and after a reload
    pg2 = _open(browser, "?view=2d")
    assert pg2.evaluate("window.office.r.kind") == "2d"
    assert pg2.evaluate("window.office.L.byUid.get('desk:planner').rot") == 90
    pg3 = _open(browser, "?view=3d")
    assert pg3.evaluate("window.office.L.props.some(p => p.id === 'custom-pottedPlant' && !p.missing)")
    # a stale page cannot overwrite a newer save
    pg.click("#editgo")
    pg.wait_for_function("window.office.edit")
    pg3.evaluate("fetch('/api/office/layout', {method: 'PUT', body: JSON.stringify({version: 1})})")
    pg.click("[data-add=floor_lamp]")
    pg.click("[data-a=save]")
    pg.wait_for_function("/saved this office meanwhile/.test(window.office.edit.err)")
    assert pg.evaluate("window.office.edit.dirty")


def test_office_endpoint_reports_nicknames_of_the_planners(tmp_path):
    bus = _bus(tmp_path)
    bus.agents["planner"].nickname = "Plan B"
    bus.agents["eng_lead"].nickname = "Lead"  # not a planner here: not reported
    o = TestClient(create_app(bus)).get("/api/office").json()
    assert o["planners"] == ["planner"] and o["nicknames"] == {"planner": "Plan B"}
    named = _bus(tmp_path, {"name": "hq-a", "planners": ["eng_lead", "planner"]}, "b")
    named.agents["eng_lead"].nickname = "Lead"
    o = TestClient(create_app(named)).get("/api/office").json()
    assert o["planners"] == ["eng_lead", "planner"] and o["nicknames"] == {"eng_lead": "Lead"}
