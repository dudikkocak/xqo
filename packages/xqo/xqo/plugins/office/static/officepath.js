/* Office view, walking: where agents can go and how they get there, on the layout's tile grid (1 tile = 1 world unit,
   c = column, r = row). A tile is blocked by the back wall, by a prop whose catalog entry blocks, or by "no-go" paint
   from office.json; "corridor" paint makes a tile cheaper, so agents prefer it. Paths are A* on 8 neighbours with no
   corner cutting, then straightened wherever an agent fits through in a straight line (without leaving a corridor).
   Used by office3d.js (walking) and officeedit.js (the walkability check). */
export const CORRIDOR = 0.5;   // a corridor tile costs half a floor tile
const RADIUS = 0.3;            // how wide an agent is, for straight lines: it keeps this far from blocked tiles

// {cols, rows, free: Uint8Array (1 = walkable), cost: Float32Array}; ok(c, r), at(c, r) = index
export function walkGrid(L) {
  const rows = L.rows, cols = L.cols, free = new Uint8Array(rows * cols).fill(1), cost = new Float32Array(rows * cols).fill(1);
  const G = {cols, rows, free, cost, at: (c, r) => r * cols + c, ok: (c, r) => c >= 0 && r >= 0 && c < cols && r < rows && free[r * cols + c] === 1};
  const block = (c, r) => {if (c >= 0 && r >= 0 && c < cols && r < rows) free[r * cols + c] = 0};
  for (const w of L.walls) {const [c, r] = w.split(",").map(Number); block(c, r)}
  for (const p of L.props) if (p.blocks)
    for (let r = Math.floor(p.y + 0.01); r < Math.ceil(p.y + p.h - 0.01); r++) for (let c = Math.floor(p.x + 0.01); c < Math.ceil(p.x + p.w - 0.01); c++) block(c, r);
  const paint = L.paint || {};
  for (const k of paint.block || []) {const [c, r] = k.split(",").map(Number); block(c, r)}
  for (const k of paint.path || []) {const [c, r] = k.split(",").map(Number); if (c >= 0 && r >= 0 && c < cols && r < rows) cost[r * cols + c] = CORRIDOR}
  return G;
}

// t, or the closest walkable tile to it for which want(c, r) holds (t itself when nothing fits nearby)
export function nearest(G, t, want = () => true) {
  if (G.ok(t.c, t.r) && want(t.c, t.r)) return t;
  for (let d = 1; d < 8; d++) {
    let best = null, bd = Infinity;
    for (let dr = -d; dr <= d; dr++) for (let dc = -d; dc <= d; dc++) {
      if (Math.max(Math.abs(dr), Math.abs(dc)) !== d) continue;
      const c = t.c + dc, r = t.r + dr, k = dc * dc + dr * dr;
      if (k < bd && G.ok(c, r) && want(c, r)) {best = {c, r}; bd = k}
    }
    if (best) return best;
  }
  return t;
}

// a binary heap of [priority, value]
function heap() {
  const a = [];
  return {
    get size() {return a.length},
    push(p, v) {a.push([p, v]); let i = a.length - 1; while (i) {const j = (i - 1) >> 1; if (a[j][0] <= a[i][0]) break; [a[i], a[j]] = [a[j], a[i]]; i = j}},
    pop() {
      const top = a[0], last = a.pop();
      if (a.length) {a[0] = last; let i = 0; for (;;) {const l = 2 * i + 1, r = l + 1; let m = i;
        if (l < a.length && a[l][0] < a[m][0]) m = l; if (r < a.length && a[r][0] < a[m][0]) m = r; if (m === i) break; [a[i], a[m]] = [a[m], a[i]]; i = m}}
      return top;
    },
  };
}

// the tiles from a to b (without a, with b), or null when b cannot be reached. avoid: a Set of "c,r" to walk around
// (agents standing there); the goal itself is never avoided
export function findPath(G, a, b, avoid = null) {
  const {cols, cost} = G, start = G.at(a.c, a.r), goal = G.at(b.c, b.r);
  if (start === goal) return [];
  if (!G.ok(b.c, b.r)) return null;
  const pass = (c, r) => G.ok(c, r) && !(avoid && avoid.has(`${c},${r}`) && G.at(c, r) !== goal);
  const h = (c, r) => {const dx = Math.abs(c - b.c), dy = Math.abs(r - b.r); return (Math.max(dx, dy) + 0.414 * Math.min(dx, dy)) * CORRIDOR};
  const dist = new Float64Array(G.rows * cols).fill(Infinity), prev = new Int32Array(G.rows * cols).fill(-1), open = heap();
  dist[start] = 0; open.push(h(a.c, a.r), start);
  while (open.size) {
    const [f, k] = open.pop();
    if (k === goal) break;
    const c = k % cols, r = (k - c) / cols;
    if (f - h(c, r) > dist[k] + 1e-9) continue;  // a stale entry
    for (let dr = -1; dr <= 1; dr++) for (let dc = -1; dc <= 1; dc++) {
      if (!dr && !dc) continue;
      const nc = c + dc, nr = r + dr;
      if (!pass(nc, nr) || (dr && dc && (!pass(nc, r) || !pass(c, nr)))) continue;
      const nk = G.at(nc, nr), nd = dist[k] + (dr && dc ? 1.414 : 1) * (cost[k] + cost[nk]) / 2;
      if (nd < dist[nk]) {dist[nk] = nd; prev[nk] = k; open.push(nd + h(nc, nr), nk)}
    }
  }
  if (prev[goal] < 0) return null;
  const out = []; for (let k = goal; k !== start; k = prev[k]) out.push({c: k % cols, r: Math.floor(k / cols)});
  return out.reverse();
}

// can an agent walk straight from tile a's centre to tile b's centre: every tile within RADIUS of the line is walkable
// (and not avoided), and none costs more than maxCost (so a straight line does not leave a corridor)
export function lineClear(G, a, b, avoid = null, maxCost = Infinity) {
  const x0 = a.c + 0.5, y0 = a.r + 0.5, dx = b.c - a.c, dy = b.r - a.r, n = Math.max(1, Math.ceil(Math.hypot(dx, dy) / 0.1));
  for (let i = 0; i <= n; i++) {
    const x = x0 + dx * i / n, y = y0 + dy * i / n;
    if (G.cost[G.at(Math.floor(x), Math.floor(y))] > maxCost) return false;
    for (const [ox, oy] of [[-RADIUS, -RADIUS], [RADIUS, -RADIUS], [-RADIUS, RADIUS], [RADIUS, RADIUS]]) {
      const c = Math.floor(x + ox), r = Math.floor(y + oy);
      if (!G.ok(c, r) || (avoid && avoid.has(`${c},${r}`) && !(c === b.c && r === b.r))) return false;
    }
  }
  return true;
}

// a path from findPath with the corners that are not needed taken out: from where the agent is, the farthest tile
// it can walk to in a straight line, and so on
export function smooth(G, from, tiles, avoid = null) {
  if (tiles.length < 2) return tiles;
  const out = [];
  let cur = from, i = 0;
  while (i < tiles.length) {
    let j = i, top = G.cost[G.at(tiles[i].c, tiles[i].r)];
    for (let k = i + 1; k < tiles.length; k++) {
      top = Math.max(top, G.cost[G.at(tiles[k].c, tiles[k].r)]);
      if (!lineClear(G, cur, tiles[k], avoid, top)) break;
      j = k;
    }
    out.push(tiles[j]); cur = tiles[j]; i = j + 1;
  }
  return out;
}

// which walkable tiles connect: a label per tile (0 = blocked), and the label of the biggest area
export function areas(G) {
  const label = new Int32Array(G.rows * G.cols), size = [0];
  for (let s = 0; s < label.length; s++) {
    if (!G.free[s] || label[s]) continue;
    const id = size.length; let n = 0; const stack = [s]; label[s] = id;
    while (stack.length) {
      const k = stack.pop(), c = k % G.cols, r = (k - c) / G.cols; n++;
      for (const [dc, dr] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
        const nc = c + dc, nr = r + dr;
        if (G.ok(nc, nr) && !label[G.at(nc, nr)]) {label[G.at(nc, nr)] = id; stack.push(G.at(nc, nr))}
      }
    }
    size.push(n);
  }
  let main = 0; for (let i = 1; i < size.length; i++) if (size[i] > size[main]) main = i;
  return {label, main};
}

// the spots agents cannot reach from the main floor: [{name, type, uid, agent, c, r}] (agent: whose desk seat it is)
export function unreachable(L, G = walkGrid(L)) {
  const {label, main} = areas(G), owner = new Map([...(L.homes || new Map())].map(([agent, spot]) => [spot, agent])), out = [];
  for (const p of L.props) for (const a of p.anchors || []) {
    const t = nearest(G, {c: Math.round(p.x + a.ox), r: Math.round(p.y + a.oy)});
    if (!G.ok(t.c, t.r) || label[G.at(t.c, t.r)] !== main) out.push({name: a.name, type: a.type, uid: p.parent || p.uid, agent: owner.get(a.name) || null, c: t.c, r: t.r});
  }
  return out;
}
