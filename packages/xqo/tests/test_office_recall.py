"""A click on an agent's monitor (or on its desk, also one without a computer) opens its panel and calls it back to its seat."""
import json
import shutil
import subprocess
import time

import pytest

from test_office_idle import ARRIVED, STATIC, _Office, office_page  # noqa: F401  (the browser fixture)

JS = r"""
import {layout, desksOf, deskAgent} from "%s/officelayout.js";
const CAT = JSON.parse(await (await import("node:fs/promises")).readFile("%s/office3d/catalog.json", "utf8"));
const zones = [{key: "eng", label: "eng", members: [{id: "a"}, {id: "b"}]}];
const L = layout(zones, {version: 1}, CAT, {});
const d = desksOf(L, ["a", "b", "nobody"]);
const out = {n: d.size, a: deskAgent(d, "desk:a", 0, 0), b: deskAgent(d, "desk:b", 0, 0), chair: deskAgent(d, L.byUid.get("desk:a/0").parent, 0, 0),
  shelf: deskAgent(d, "shelf:eng", 0, 0)};
// two seats on one desk: the nearer one wins
const two = new Map([["t", [{id: "x", x: 1, y: 2}, {id: "y", x: 5, y: 2}]]]);
out.near = [deskAgent(two, "t", 1.2, 1), deskAgent(two, "t", 4.6, 1)];
// a desk without a work spot (its catalog entry has none): the agent's own automatic desk still counts
const bare = {...CAT, props: {...CAT.props, wooden_desk_single: {...CAT.props.plain_desk}}};
const L2 = layout(zones, {version: 1}, bare, {});
out.bare = deskAgent(desksOf(L2, ["a", "b"]), "desk:b", 0, 0);
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_desks_of_agents(tmp_path):
    f = tmp_path / "t.mjs"
    f.write_text(JS % (STATIC, STATIC))
    r = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    o = json.loads(r.stdout)
    assert o == {"n": 2, "a": "a", "b": "b", "chair": "a", "shelf": None, "near": ["x", "y"], "bare": "b"}


# a desk without a computer: the agents' desks are the glb desk (with the seat spot, so they still sit there)
PLAIN = {"version": 1, "catalog": {"props": {"wooden_desk_single": {
    "label": "Desk (no computer)", "glb": "furniture/desk.glb", "scale": 3.4, "yscale": 0.75, "size": [3, 2], "blocks": True,
    "anchors": [{"type": "work", "ox": 1, "oy": 2}], "sprite2d": "wooden_desk_single", "tags": ["work"]}}}}


@pytest.fixture
def recall_page(office_page, tmp_path):
    def make(view, layout=None):
        if layout is not None:  # office.json in the artifacts dir of the next office the fixture starts
            n = 0
            while (tmp_path / f"o{n}").exists():
                n += 1
            (tmp_path / f"pre{n}").mkdir()
            (tmp_path / f"pre{n}" / "office.json").write_text(json.dumps(layout))
            orig = _Office.__init__

            def init(self, path, office=None):
                (path / "a").mkdir(parents=True, exist_ok=True)
                shutil.copy(tmp_path / f"pre{n}" / "office.json", path / "a" / "office.json")
                orig(self, path, office)
            _Office.__init__ = init
            try:
                pg, srv = office_page(view)
            finally:
                _Office.__init__ = orig
        else:
            pg, srv = office_page(view)
        if view == "3d":
            try:
                pg.wait_for_function("office.r.kind === '3d' && office.r.idle && office.r.idle.spots.length", timeout=15000)
            except Exception:
                pytest.skip("WebGL is not available in this browser")
        return pg, srv
    return make


def _away_agent(pg, view):
    pg.wait_for_function(f"({ARRIVED[view]}).length >= 1", timeout=60000)  # somebody is away from its desk, pretending
    return pg.evaluate(f"({ARRIVED[view]})[0][0]")


def _click_desk(pg, aid, monitor):
    pos = pg.evaluate(f"office.r.deskPos({json.dumps(aid)})")
    assert pos and pos["monitor"] is monitor
    pg.mouse.click(pos["x"], pos["y"])


@pytest.mark.parametrize("view", ["2d", "3d"])
@pytest.mark.parametrize("monitor", [True, False])
def test_desk_click_opens_panel_and_calls_agent_back(recall_page, view, monitor):
    pg, srv = recall_page(view, None if monitor else PLAIN)
    aid = _away_agent(pg, view)
    assert not pg.evaluate(f"office.r.atDesk({json.dumps(aid)})")
    _click_desk(pg, aid, monitor)
    assert pg.evaluate("office.sel") == aid  # its side panel
    assert not pg.evaluate("document.body.classList.contains('nopanel')")
    pg.wait_for_function(f"!office.r.idle.plans.has({json.dumps(aid)})", timeout=5000)  # it stops pretending at once
    pg.wait_for_function(f"office.r.atDesk({json.dumps(aid)})", timeout=30000)  # and walks back to its seat
    for _ in range(3):  # where it stays while the panel is open
        time.sleep(1)
        assert pg.evaluate(f"office.r.atDesk({json.dumps(aid)})") and not pg.evaluate(f"office.r.idle.plans.has({json.dumps(aid)})")
    pg.click("#pclose")  # closing the panel lets it go
    assert pg.evaluate(f"office.cit.get({json.dumps(aid)}).recall") == 0


@pytest.mark.parametrize("view", ["2d", "3d"])
def test_recall_ends_after_a_while(recall_page, view):
    pg, srv = recall_page(view)
    aid = _away_agent(pg, view)
    pg.evaluate("office.recallMs = 1500")
    _click_desk(pg, aid, True)
    assert pg.evaluate(f"office.cit.get({json.dumps(aid)}).recall") > 0
    time.sleep(2)
    assert pg.evaluate("office.sel") == aid  # the panel stays open; the agent is free to wander again
    assert pg.evaluate(f"office.cit.get({json.dumps(aid)}).recall < Date.now()")


def test_a_working_agent_is_not_called_back(recall_page):
    pg, srv = recall_page("2d")
    aid = pg.evaluate("[...office.cit.keys()][0]")
    pg.evaluate(f"(() => {{const c = office.cit.get({json.dumps(aid)}); c.busy = 1; c.flash = {{state: 'working', until: Date.now() + 60000}}}})()")  # working: at its desk already, only the panel opens
    _click_desk(pg, aid, True)
    assert pg.evaluate("office.sel") == aid and pg.evaluate(f"office.cit.get({json.dumps(aid)}).recall") == 0
