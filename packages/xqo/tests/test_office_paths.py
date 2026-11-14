"""Office view, walking: floor paint and waypoints in office.json, the path finder, the walkability check in Edit mode,
and agents that do not stop on one another."""
import pytest

from xqo.plugins.office.view import validate_layout
from test_office import _open, browser  # noqa: F401 (browser: the fixture)


def test_paint_and_waypoints_in_the_layout_file():
    doc = {"paint": {"engineering": {"block": [[1, 2], [1, 2], [0, 0]], "path": [[3, 4]]}, "@lounge": {"path": []}},
           "catalog": {"props": {"spot": {"builtin": "waypoint", "marker": True, "yscale": 0.5,
                                          "anchors": [{"type": "wander", "ox": 0.5, "oy": 0.5}]}}}}
    out = validate_layout(doc)
    assert out["paint"] == {"engineering": {"block": [[0, 0], [1, 2]], "path": [[3, 4]]}, "@lounge": {"path": []}}
    assert out["catalog"]["props"]["spot"]["anchors"][0]["type"] == "wander" and out["catalog"]["props"]["spot"]["marker"] is True
    for bad, msg in [({"paint": {"x": {"wall": [[0, 0]]}}}, "unknown"), ({"paint": {"x": {"block": [[0.5, 1]]}}}, "whole tiles"),
                     ({"paint": {"x": {"block": [[1]]}}}, "[x, y]"), ({"paint": {"x": {"block": "1,2"}}}, "list"),
                     ({"paint": {"x": {"block": [[0, 0]] * 20001}}}, "at most")]:
        with pytest.raises(ValueError, match=msg.replace("[", r"\[").replace("]", r"\]")):
            validate_layout(bad)


def _3d(browser):  # noqa: F811
    pg = _open(browser, "?view=3d")
    if pg.evaluate("window.office.r.kind") != "3d":
        pytest.skip("no WebGL in this browser")
    return pg


def test_path_finder(browser):  # noqa: F811
    pg = _open(browser, "?view=2d")
    r = pg.evaluate("""import('/plugins/office/officepath.js').then(m => {
        // a 12 x 8 room with a wall from (5, 0) to (5, 5): the way round goes below it
        const L = {cols: 12, rows: 8, walls: new Set(), props: [{x: 5, y: 0, w: 1, h: 6, blocks: true, anchors: []}], paint: {block: new Set(), path: new Set()}};
        const G = m.walkGrid(L), p = m.findPath(G, {c: 1, r: 1}, {c: 10, r: 1});
        const open = m.walkGrid({...L, props: []});
        const straight = m.smooth(open, {c: 1, r: 1}, m.findPath(open, {c: 1, r: 1}, {c: 10, r: 6}));
        // a corridor along row 6 is taken even though the straight way is shorter
        const C = m.walkGrid({...L, props: [], paint: {block: new Set(), path: new Set([...Array(12).keys()].flatMap(c => [`${c},6`, `${c},7`]))}});
        const viaCorridor = m.findPath(C, {c: 0, r: 5}, {c: 11, r: 5});
        // no-go paint closes the gap below the wall: the far side cannot be reached
        const shut = m.walkGrid({...L, paint: {block: new Set(['5,6', '5,7']), path: new Set()}});
        return {below: p.some(t => t.r >= 6), crossed: p.some(t => t.c === 5 && t.r < 6), end: p[p.length - 1],
                straight: straight.length, corridor: viaCorridor.filter(t => t.r >= 6).length, shut: m.findPath(shut, {c: 1, r: 1}, {c: 10, r: 1}),
                avoid: m.findPath(open, {c: 0, r: 0}, {c: 2, r: 0}, new Set(['1,0'])).some(t => t.c === 1 && t.r === 0)};
    })""")
    assert r["below"] and not r["crossed"] and r["end"] == {"c": 10, "r": 1}
    assert r["straight"] == 1  # nothing in the way: one straight walk
    assert r["corridor"] >= 8 and r["shut"] is None and not r["avoid"]


def test_waypoints_and_paint_follow_their_rooms(browser):  # noqa: F811
    pg = _open(browser, "?view=2d")
    r = pg.evaluate("""import('/plugins/office/officelayout.js').then(m => {
        const zones = () => [{key: 'a', label: 'a', members: [{id: 'x'}]}, {key: 'b', label: 'b', members: [{id: 'y'}]}];
        const cat = window.office.cat, base = m.layout(zones(), {}, cat);
        const O = {props: {'wander:lounge': {hide: true}, 'wander:hall': {x: 5, y: 2}}, paint: {b: {block: [[0, 0]]}, gone: {block: [[1, 1]]}}};
        const L = m.layout(zones(), O, cat), wide = m.layout(zones(), {...O, rooms: {a: {size: [20, 8]}}}, cat);
        const b = z => z.origins.b;
        return {auto: base.wander.length, wander: L.wander.length, hall: L.wander.find(w => w.name === 'wander:hall#0'),
                paint: [...L.paint.block], moved: [...wide.paint.block], b: [b(L).x, b(L).y], b2: [b(wide).x, b(wide).y]};
    })""")
    assert r["auto"] == 6 and r["wander"] == 5 and r["hall"] == {"name": "wander:hall#0", "x": 5, "y": 2}
    assert r["paint"] == [f"{r['b'][0]},{r['b'][1]}"] and r["moved"] == [f"{r['b2'][0]},{r['b2'][1]}"] and r["b"] != r["b2"]


def test_edit_mode_paints_and_warns_about_unreachable_desks(browser):  # noqa: F811
    pg = _3d(browser)
    pg.click("#editgo")
    pg.wait_for_function("window.office.edit && document.querySelector('#panel h3')")
    assert pg.evaluate("window.office.L.props.filter(p => p.marker).length") == 6  # the waypoints show while editing
    pg.click("[data-tool=block]")
    assert pg.evaluate("window.office.r.tool") == "block" and pg.evaluate("window.office.edit.tool") == "block"
    seat = pg.evaluate("(() => {const d = window.office.L.byUid.get('desk:planner'), a = d.anchors[0]; return [d.x + a.ox, d.y + a.oy]})()")
    c, r = seat
    pg.evaluate(f"window.office.editor.paint('block', [[{c - 1}, {r}], [{c + 1}, {r}], [{c - 1}, {r + 1}], [{c}, {r + 1}], [{c + 1}, {r + 1}]])")
    pg.wait_for_function("/planner's desk cannot be reached/.test(document.querySelector('#panel').textContent)")
    pg.keyboard.press("Control+z")
    pg.wait_for_function("!/cannot be reached/.test(document.querySelector('#panel').textContent)")
    pg.keyboard.press("Control+y")
    pg.evaluate(f"window.office.editor.paint('erase', [[{c - 1}, {r}], [{c - 1}, {r + 1}], [{c}, {r + 1}], [{c + 1}, {r + 1}]])")  # open it again
    pg.evaluate(f"window.office.editor.paint('path', [[{c}, {r + 1}], [{c - 1}, {r + 1}]])")
    pg.wait_for_function("!/cannot be reached/.test(document.querySelector('#panel').textContent)")
    pg.keyboard.press("Escape")
    assert pg.evaluate("window.office.r.tool") is None
    pg.click("[data-a=save]")
    pg.wait_for_function("!window.office.edit.dirty")
    saved = pg.evaluate("fetch('/api/office/layout').then(r => r.json())")["layout"]
    count = lambda k: sum(len(z.get(k, [])) for z in saved["paint"].values())  # noqa: E731 (tiles below a room are the hall's)
    assert count("block") == 1 and count("path") == 2
    pg.click("[data-a=done]")
    pg.wait_for_function("!window.office.edit && window.office.r && window.office.r.agents && window.office.r.agents.get('planner') && window.office.r.agents.get('planner').grp.visible")
    assert not pg.evaluate("window.office.r.scene.children.some(o => o.visible && o.userData && o.userData.marker)")


def test_agents_keep_to_their_own_tiles(browser):  # noqa: F811
    pg = _3d(browser)
    shared = 0
    for _ in range(12):
        pg.wait_for_timeout(400)
        tiles = pg.evaluate("[...window.office.r.agents.values()].filter(a => !a.path.length).map(a => a.tile.c + ',' + a.tile.r)")
        shared += len(tiles) - len(set(tiles))
    assert shared == 0
    claims = pg.evaluate("[...window.office.r.claims.values()]")
    assert len(claims) == len(set(claims)) == pg.evaluate("window.office.r.agents.size")
