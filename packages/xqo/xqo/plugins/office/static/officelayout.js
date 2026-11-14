/* Office view, layout: where every room, prop and desk is, for both renderers. The automatic layout gives each
   department (or HQ) a room with a desk per member, plus a lounge along the bottom; an HQ's edited office
   (office.json, see xqo/plugins/office/view.py) is applied on top of it: moved, turned or hidden props by uid, added
   props, room sizes, each agent's desk and character, and floor paint (no-go and corridor tiles, for walking; see
   officepath.js). Idle agents wander between waypoints, which are props too (moved, hidden or added like any other).
   Props come from the catalog (/plugins/office/office3d/catalog.json
   plus office.json's own entries). Coordinates are tiles: x = column, y = row; a prop's x / y is the top-left of
   its footprint after turning; in office.json they count from the top-left of the prop's zone. */
export const LOUNGE = "@lounge", BUILDING = "@building";

const UNKNOWN = {label: "missing model", builtin: "placeholder", size: [1, 1], blocks: true, missing: true};
export const entry = (CAT, type) => CAT.props[type] || {...UNKNOWN, label: `${type} (not in the catalog)`};
export const turned = (e, rot) => rot % 180 ? [e.size[1], e.size[0]] : [e.size[0], e.size[1]];

// a point of the prop's own (unturned) frame, in tiles from its top-left, after turning the prop by rot degrees
// clockwise (seen from above) about its centre; returned from the turned footprint's top-left
export function turn(px, py, w0, h0, rot) {
  const dx = px - w0 / 2, dy = py - h0 / 2, a = rot * Math.PI / 180, c = Math.round(Math.cos(a)), s = Math.round(Math.sin(a));
  const [w, h] = rot % 180 ? [h0, w0] : [w0, h0];
  return [dx * c - dy * s + w / 2, dx * s + dy * c + h / 2];
}

/* zones: [{key, label, members: [{id, ...}], ...}]; O: office.json ({} for automatic); CAT: {props, characters}
   opts.edit: apply O (only your own office is edited; the planners world is always automatic) */
export function layout(zones, O, CAT, opts = {}) {
  O = opts.edit === false ? {} : (O || {});
  const rooms = O.rooms || {};
  const dims = zones.map(z => {const n = Math.max(1, z.members.length), per = Math.min(3, n);
    const auto = {w: Math.max(9, 1 + per * 4), h: 2 + Math.ceil(n / per) * 4}, s = rooms[z.key] && rooms[z.key].size;
    return {w: s ? s[0] : auto.w, h: s ? s[1] : auto.h, per, auto}});
  const maxW = Math.max(...dims.map(d => d.w + 2), Math.min(64, dims.reduce((k, d) => k + d.w + 1, 1)));  // one row while it fits
  let x = 1, y = 3, rowH = 0, right = 18;
  zones.forEach((z, i) => {const d = dims[i];
    if (x > 1 && x + d.w + 1 > maxW) {x = 1; y += rowH + 1; rowH = 0}
    Object.assign(z, {x, y, w: d.w, h: d.h, per: d.per, autoSize: [d.auto.w, d.auto.h]}); x += d.w + 1; rowH = Math.max(rowH, d.h); right = Math.max(right, x)});
  let cols = right + 1;
  const Lrow = y + rowH + 1;
  const origins = {[BUILDING]: {x: 0, y: 0}, [LOUNGE]: {x: 0, y: Lrow}};
  for (const z of zones) origins[z.key] = {x: z.x, y: z.y};

  // the automatic props, by uid (stable across rebuilds: named after their room or agent)
  const auto = [];
  const P = (uid, type, zone, px, py, extra) => auto.push({uid, type, zone, x: px, y: py, rot: 0, ...extra});
  for (let c = 2, i = 0; c + 3 < cols; c += 7, i++) P(`window:${i}`, "large_window", BUILDING, c, 0);
  for (const z of zones) {
    P(`plant:${z.key}`, "tall_potted_plant", z.key, 0, 0);
    P(`board:${z.key}`, "wooden_framed_whiteboard", z.key, (z.w - 2) / 2, 0);
    P(`shelf:${z.key}`, "bookshelf_packed", z.key, z.w - 2.3, 0);
    const off = Math.floor((z.w - (1 + z.per * 4)) / 2);
    z.members.forEach((m, k) => P(`desk:${m.id}`, "wooden_desk_single", z.key, 1 + off + (k % z.per) * 4, 2 + Math.floor(k / z.per) * 4, {agent: m.id}));
  }
  const cx = Math.floor(cols / 2);  // lounge along the bottom: where idle agents hang out
  P("coffee_bar", "coffee_bar_counter", LOUNGE, 1, 0);
  P("fridge", "mini_fridge", LOUNGE, 4, 0.2);
  P("sofa", "sofa", LOUNGE, cx - 2.5, 0);  // four seats facing the rug: where idle agents sit and chat
  P("rug", "area_rug_lounge", LOUNGE, cx - 2, 2);
  P("coffee_table", "low_coffee_table", LOUNGE, cx - 0.75, 2.75);
  P("beanbag_l", "bean_bag_chair_dark", LOUNGE, cx - 3.5, 2.5);
  P("beanbag_r", "bean_bag_chair_light", LOUNGE, cx + 2, 2.5);
  P("shelf:lounge", "bookshelf_packed", LOUNGE, cols - 4, 0);
  P("plant:lounge", "tall_potted_plant", LOUNGE, cols - 2, 1);
  // waypoints: where idle agents wander (in the lounge, and along the hall in front of the back wall)
  for (const [name, x, zone, y] of [["lounge", cx, LOUNGE, 4], ["left", 3, LOUNGE, 4], ["right", cols - 3, LOUNGE, 4],
    ["hall", cx, BUILDING, 2], ["hall_l", 3, BUILDING, 2], ["hall_r", cols - 3, BUILDING, 2]]) P(`wander:${name}`, "waypoint", zone, x, y);

  // apply office.json: changes to automatic props, then the added ones
  const changes = O.props || {}, hidden = [], orphans = [], seeds = [];
  for (const p of auto) {
    const o = changes[p.uid];
    if (o && o.hide) {hidden.push(p.uid); continue}
    seeds.push({...p, ...(o ? {x: o.x ?? p.x, y: o.y ?? p.y, rot: o.rot ?? p.rot} : {}), auto: true, changed: !!o});
  }
  for (const a of O.added || []) {
    if (!origins[a.zone]) {orphans.push(a.uid); continue}  // its department is gone: kept in office.json, not drawn
    seeds.push({...a, auto: false, changed: true});
  }

  const props = [], byUid = new Map();
  const make = (s, X, Y, rot, parent) => {
    const e = entry(CAT, s.type), [w, h] = turned(e, rot), [w0, h0] = e.size;
    const p = {uid: s.uid, id: s.type, x: X, y: Y, w, h, w0, h0, rot, zone: s.zone, auto: !!s.auto, changed: !!s.changed,
      agent: s.agent || null, parent: parent || null, layer: e.layer || "below", blocks: !!e.blocks, missing: !!e.missing, marker: !!e.marker, anchors: []};
    const sits = (e.anchors || []).filter(a => a.act === "sit");
    (e.anchors || []).forEach((a, i) => {  // the spot's tile, turned with the prop
      const [tx, ty] = turn(Math.floor(a.ox) + 0.5, Math.floor(a.oy) + 0.5, w0, h0, rot);
      let c = Math.floor(X + tx), r = Math.floor(Y + ty);
      // a spot outside the prop stays outside when the prop sits on half tiles (which block the whole tile)
      const inside = () => c >= Math.floor(X + 0.01) && c < Math.ceil(X + w - 0.01) && r >= Math.floor(Y + 0.01) && r < Math.ceil(Y + h - 0.01);
      const out = tx < 0 || tx > w || ty < 0 || ty > h;
      if (out && inside()) {const dx = tx - w / 2, dy = ty - h / 2; if (Math.abs(dx) >= Math.abs(dy)) c += Math.sign(dx); else r += Math.sign(dy)}
      const A = {name: `${s.uid}#${i}`, type: a.type, act: a.act || null, ox: c - X, oy: r - Y, rot, prop: s.uid, fx: X + w / 2, fy: Y + h / 2};
      if (a.act === "sit") {  // seats share the cushion evenly (between the arms), a little forward of the backrest
        const [u, v] = turn((0.25 + (w0 - 0.5) * (sits.indexOf(a) + 0.5) / sits.length), h0 / 2 + 0.12, w0, h0, rot);
        A.sx = X + u; A.sy = Y + v;
      }
      p.anchors.push(A);  // act: what idle agents pretend to do here; fx/fy: the prop's centre, to face it
    });
    props.push(p); byUid.set(p.uid, p);
    (e.with || []).forEach((c, i) => {  // props that come along (a desk's chair), turned with it
      const ce = entry(CAT, c.type), crot = (rot + (c.rot || 0)) % 360, [cw, ch] = turned(ce, crot);
      const [tx, ty] = turn(c.x + ce.size[0] / 2, c.y + ce.size[1] / 2, w0, h0, rot);
      make({uid: `${s.uid}/${i}`, type: c.type, zone: s.zone}, X + tx - cw / 2, Y + ty - ch / 2, crot, s.uid);
    });
    return p;
  };
  for (const s of seeds) {const o = origins[s.zone]; make(s, o.x + s.x, o.y + s.y, s.rot || 0)}

  // desks: an agent's own automatic desk, or the desk office.json gives it
  const homes = new Map(), agents = O.agents || {};
  for (const p of props) if (p.agent && p.anchors[0]) homes.set(p.agent, p.anchors[0].name);
  for (const [id, a] of Object.entries(agents)) {
    const d = a.desk && byUid.get(a.desk), seat = d && d.anchors.find(x => x.type === "work");
    if (seat && zones.some(z => z.members.some(m => m.id === id))) homes.set(id, seat.name);
  }

  // the building grows to hold everything that was moved outwards
  let rows = Lrow + 6;
  for (const p of props) {cols = Math.max(cols, Math.ceil(p.x + p.w) + 1); rows = Math.max(rows, Math.ceil(p.y + p.h) + 1)}
  const floor = Array.from({length: rows}, (_, r) => Array.from({length: cols}, () => r < 2 ? "exposed_brick_wall" : "concrete_accent"));
  for (const z of zones) for (let r = z.y; r < z.y + z.h && r < rows; r++) for (let c = z.x; c < z.x + z.w && c < cols; c++) floor[r][c] = "main_floor";
  const walls = new Set();
  for (let c = 0; c < cols; c++) {walls.add(`${c},0`); walls.add(`${c},1`)}
  const wander = props.flatMap(p => p.anchors.filter(a => a.type === "wander").map(a => ({name: a.name, x: p.x + a.ox, y: p.y + a.oy})));
  // floor paint: tiles from the top-left of their zone, like props (paint of a department that is gone is kept, not used)
  const paint = {block: new Set(), path: new Set()};
  for (const [zone, z] of Object.entries(O.paint || {})) {
    const o = origins[zone]; if (!o) continue;
    for (const k of ["block", "path"]) for (const [x, y] of z[k] || []) {
      const c = o.x + x, r = o.y + y;
      if (c >= 0 && r >= 0 && c < cols && r < rows) paint[k].add(`${c},${r}`);
    }
  }
  return {cols, rows, floor, props, byUid, walls, wander, paint, homes, origins, lounge: Lrow, hidden, orphans,
    theme: O.theme || {}, characters: Object.fromEntries(Object.entries(agents).filter(([, a]) => a.character).map(([k, a]) => [k, a.character]))};
}

// which zone a tile belongs to: a room, the lounge (below the rooms), or the building
export function zoneAt(L, zones, tx, ty) {
  const z = zones.find(z => tx >= z.x && tx < z.x + z.w && ty >= z.y && ty < z.y + z.h);
  return z ? z.key : ty >= L.lounge ? LOUNGE : BUILDING;
}

/* the agents' desks: uid of a desk -> [{id, x, y}] (its seat, the tile centre). A desk is the prop with the agent's home
   spot, or (when that prop has no work spot, like a desk without a computer) the agent's own automatic desk. Its chair and
   other `with` props count as the desk: look a prop up by `p.parent || p.uid`. */
export function desksOf(L, ids) {
  const out = new Map(), add = (uid, s) => (out.get(uid) || out.set(uid, []).get(uid)).push(s);
  for (const id of ids) {
    const home = L.homes.get(id);
    const p = home && L.props.find(p => p.anchors.some(a => a.name === home));
    if (p) {const a = p.anchors.find(a => a.name === home); add(p.parent || p.uid, {id, x: p.x + a.ox + 0.5, y: p.y + a.oy + 0.5}); continue}
    const d = L.props.find(p => p.agent === id && !p.parent);
    if (d) add(d.uid, {id, x: d.x + d.w / 2, y: d.y + d.h});
  }
  return out;
}
// the agent at the desk `uid` nearest to (x, y) in tiles (a desk with several seats), or null
export function deskAgent(desks, uid, x, y) {
  const seats = desks.get(uid); if (!seats) return null;
  return seats.reduce((b, s) => Math.hypot(s.x - x, s.y - y) < Math.hypot(b.x - x, b.y - y) ? s : b).id;
}
