/* Office view, 3D renderer: the same office as office2d.js, as a low-poly scene in three.js.
   Desks, chairs, screens, plants and characters are Kenney models (CC0, in /plugins/office/office3d/); the rest of the
   furniture is built here from simple shapes in the same style. What can be placed, and how, is the catalog
   (/plugins/office/office3d/catalog.json plus an HQ's own entries; officelayout.js places it). It only draws what office.js
   decides: who is working, paused, waiting for you or messaging whom. Clicks go back through ctx.onPick / ctx.onZone.
   In Edit mode (setEdit) the agents step out and props can be picked and dragged; office.js keeps the layout.
   Agents walk on the same tile grid as the 2D office (1 tile = 1 world unit, x = column, z = row; officepath.js finds
   the way). Each heads for a tile no other agent heads for, walks around agents that stand in its way, and steps a
   little aside for agents walking past. In Edit mode the floor shows where agents can walk (and which spots they
   cannot reach), waypoints show up, and floor paint (no-go, corridor) is brushed on with setTool. */
import * as THREE from "three";
import {GLTFLoader} from "/plugins/office/vendor/three/addons/loaders/GLTFLoader.js";
import {OrbitControls} from "/plugins/office/vendor/three/addons/controls/OrbitControls.js";
import {clone as cloneSkinned} from "/plugins/office/vendor/three/addons/utils/SkeletonUtils.js";
import {entry, desksOf, deskAgent} from "/plugins/office/officelayout.js";
import {walkGrid, nearest, findPath, smooth, unreachable} from "/plugins/office/officepath.js";
import {IdleDirector, ACTS as IDLE_ACTS, CHAT} from "/plugins/office/officeidle.js";

const ART = "/plugins/office/office3d";
const K = 3.4;                 // Kenney furniture kit units -> tiles
const CHAR_H = 2.0;           // character height in tiles
const SPEED = 3.2;             // tiles per second
const WALL_H = 2.6;
const SEAT = {y: 0.78, z: -0.35};   // where a seated agent sits, from its tile centre: on a desk chair's cushion
const DESK_Y = 0.75;                // the desk is lower than the kit's, to suit the characters' size
const REST = {y: 0.2, z: -0.95};    // and a beanbag (north of its tile)
const SOFA_Y = 0.7;                   // a sofa seat: hips above the cushion (its top is 0.54), feet over its front edge
const CORE = ["desk", "chairDesk", "computerScreen", "computerKeyboard", "computerMouse"];  // the built-in desk and chair use these
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const rand = (a, b) => a + Math.random() * (b - a);
const rad = deg => deg * Math.PI / 180;

/* ---------------- models: loaded once per file, on demand ---------------- */
const files = new Map(), M = {core: {}, props: new Map(), chars: new Map()};
const loadGLB = url => {if (!files.has(url)) files.set(url, new GLTFLoader().loadAsync(url)); return files.get(url)};
// the kit is unlit; give it shading so the office has depth (textures and vertex colours are kept)
function shade(g) {
  g.traverse(o => {if (o.isMesh) {const m = o.material;
    o.material = new THREE.MeshLambertMaterial({color: m.color ? m.color.clone() : 0xffffff, map: m.map || null, vertexColors: !!m.vertexColors,
      transparent: !!m.transparent, opacity: m.opacity ?? 1, side: m.side});
    o.castShadow = o.receiveShadow = true}});
  return g;
}
// wrap a model so its footprint is centred on x/z and it stands on y = 0
function normalize(g, k) {
  g.scale.setScalar(k); g.updateMatrixWorld(true);
  const box = new THREE.Box3().setFromObject(g), c = box.getCenter(new THREE.Vector3());
  g.position.set(-c.x, -box.min.y, -c.z);
  const w = new THREE.Group(); w.add(g);
  w.userData.size = box.getSize(new THREE.Vector3());
  return w;
}
// a catalog file: a shipped one under /plugins/office/office3d/, or assets/<file> uploaded to this HQ (ctx.asset)
const fileUrl = (ctx, f) => f.startsWith("assets/") ? ctx.asset(f.slice(7)) : `${ART}/${f}`;
async function loadCore() {
  if (M.core.desk) return;
  const gl = await Promise.all(CORE.map(n => loadGLB(`${ART}/furniture/${n}.glb`)));
  CORE.forEach((n, i) => {M.core[n] = normalize(shade(gl[i].scene.clone(true)), n === "chairDesk" ? K * 0.72 : K)});
  M.core.desk.scale.y = DESK_Y; M.core.desk.userData.size.y *= DESK_Y;  // the screen, keyboard and mouse stand on its top
  M.core.computerScreen.userData.panel = panelOf(M.core.computerScreen);
}
// the screen's front face: the biggest forward-facing flat area above the foot (the screen may lean back).
// The glow goes on that plane, a hair in front of it: {centre, normal, w, h}
function panelOf(g) {
  g.updateMatrixWorld(true);
  const size = g.userData.size, tris = [], a = new THREE.Vector3(), b = new THREE.Vector3(), c = new THREE.Vector3();
  g.traverse(o => {
    if (!o.isMesh) return;
    const pos = o.geometry.attributes.position, idx = o.geometry.index, n = idx ? idx.count : pos.count;
    const at = (v, k) => v.fromBufferAttribute(pos, idx ? idx.getX(k) : k).applyMatrix4(o.matrixWorld);
    for (let k = 0; k + 2 < n; k += 3) {
      at(a, k); at(b, k + 1); at(c, k + 2);
      const cross = new THREE.Vector3().subVectors(b, a).cross(new THREE.Vector3().subVectors(c, a)), area = cross.length() / 2;
      if (!area) continue;
      const nrm = cross.normalize(), mid = a.clone().add(b).add(c).divideScalar(3);
      if (nrm.z > 0.5 && mid.y > size.y * 0.3) tris.push({nrm, area, pts: [a.clone(), b.clone(), c.clone()], d: nrm.dot(a)});
    }
  });
  // group the triangles by plane; the plane with the most area is the screen
  const planes = [];
  for (const t of tris) {
    const p = planes.find(p => p.nrm.dot(t.nrm) > 0.995 && Math.abs(p.d - t.d) < 0.01);
    if (p) {p.area += t.area; p.pts.push(...t.pts)} else planes.push({nrm: t.nrm, d: t.d, area: t.area, pts: [...t.pts]});
  }
  const best = planes.sort((x, y) => y.area - x.area)[0];
  if (!best) return {centre: new THREE.Vector3(0, size.y * 0.58, size.z / 2), normal: new THREE.Vector3(0, 0, 1), w: 1.15, h: 0.62};
  const u = new THREE.Vector3(1, 0, 0).projectOnPlane(best.nrm).normalize(), v = new THREE.Vector3().crossVectors(best.nrm, u);
  let u0 = Infinity, u1 = -Infinity, v0 = Infinity, v1 = -Infinity;
  for (const p of best.pts) {const pu = p.dot(u), pv = p.dot(v); u0 = Math.min(u0, pu); u1 = Math.max(u1, pu); v0 = Math.min(v0, pv); v1 = Math.max(v1, pv)}
  const centre = u.clone().multiplyScalar((u0 + u1) / 2).add(v.clone().multiplyScalar((v0 + v1) / 2)).add(best.nrm.clone().multiplyScalar(best.d));
  return {centre, normal: best.nrm.clone(), w: (u1 - u0) * 0.9, h: (v1 - v0) * 0.86};
}

/* a working agent's screen: lines of code that scroll at a random pace, with the odd flicker. One drawing, shared;
   each screen has its own copy of the texture (same image), scrolled on its own. */
let code = null;
function codeTexture() {
  if (!code) {
    const c = document.createElement("canvas"); c.width = 64; c.height = 256;
    const g = c.getContext("2d"), INK = ["#7fc4ff", "#9ae6b4", "#fbd38d", "#f6a5c0", "#cbd5e0", "#cbd5e0"];
    g.fillStyle = "#0d1726"; g.fillRect(0, 0, 64, 256);
    for (let y = 3, indent = 0; y < 252; y += 7) {
      indent = Math.max(0, Math.min(4, indent + Math.floor(Math.random() * 3) - 1));
      if (Math.random() < 0.12) continue;  // an empty line
      for (let x = 4 + indent * 5, k = 0; k < 1 + Math.floor(Math.random() * 3) && x < 58; k++) {
        const w = 4 + Math.random() * 18; g.fillStyle = INK[Math.floor(Math.random() * INK.length)];
        g.fillRect(x, y, Math.min(w, 60 - x), 3); x += w + 3;
      }
    }
    code = new THREE.CanvasTexture(c); code.colorSpace = THREE.SRGBColorSpace;
  }
  const t = code.clone(); t.wrapS = t.wrapT = THREE.RepeatWrapping; t.repeat.set(1, 0.32); t.offset.y = Math.random(); t.needsUpdate = true;
  return t;
}

/* windows: a grey-blue that is lighter by day and darker at night, from the browser's local time */
const glass = new THREE.MeshLambertMaterial({color: "#9fd3f0", emissive: "#3a6f8f", flatShading: true});
const SKY = {day: [new THREE.Color("#b9c9d8"), new THREE.Color("#4f667b")], night: [new THREE.Color("#28354a"), new THREE.Color("#0f1826")]};
export function daylight(d = new Date()) {  // 0 at night, 1 by day; dawn 6-8 h, dusk 18-20 h
  const h = d.getHours() + d.getMinutes() / 60, s = x => x * x * (3 - 2 * x);
  return h < 6 || h >= 20 ? 0 : h < 8 ? s((h - 6) / 2) : h < 18 ? 1 : s((20 - h) / 2);
}
function tintWindows(d) {
  const k = daylight(d);
  glass.color.copy(SKY.night[0]).lerp(SKY.day[0], k); glass.emissive.copy(SKY.night[1]).lerp(SKY.day[1], k);
  return k;
}
// the props and characters a layout needs; a file that does not load is drawn as a placeholder
async function prepare(ctx, L) {
  await loadCore();
  const want = new Set(L.props.map(p => p.id));
  await Promise.all([...want].map(async type => {
    const e = entry(ctx.cat, type); if (!e.glb) return;
    const url = fileUrl(ctx, e.glb), key = `${url}|${e.scale || ""}|${e.yscale || ""}|${e.size}`;
    if (M.props.has(key)) return;
    try {
      const g = shade((await loadGLB(url)).scene.clone(true));
      let k = e.scale;
      if (!k) {  // no scale given: fit the footprint
        const b = new THREE.Box3().setFromObject(g), sz = b.getSize(new THREE.Vector3());
        k = Math.min(e.size[0] / (sz.x || 1), e.size[1] / (sz.z || 1)) * 0.95;
      }
      const m = normalize(g, k);
      if (e.yscale) {m.scale.y = e.yscale; m.userData.size.y *= e.yscale}
      M.props.set(key, m);
    } catch (err) {console.warn(`office: ${url} did not load`, err); M.props.set(key, null)}
  }));
  const chars = new Set([...ctx.characters, ...Object.values(L.characters || {})]);
  await Promise.all([...chars].map(async id => {
    const c = ctx.cat.characters[id]; if (!c || M.chars.has(id)) return;
    try {
      const gl = await loadGLB(fileUrl(ctx, c.glb)), box = new THREE.Box3().setFromObject(gl.scene);
      M.chars.set(id, {scene: gl.scene, scale: CHAR_H / ((box.max.y - box.min.y) || 1), clips: Object.fromEntries(gl.animations.map(a => [a.name, a]))});
    } catch (err) {console.warn(`office: character ${id} did not load`, err)}
  }));
}
const propModel = (ctx, e) => M.props.get(`${fileUrl(ctx, e.glb)}|${e.scale || ""}|${e.yscale || ""}|${e.size}`);
const mats = new Map();
function mat(color, extra) {
  const key = color + JSON.stringify(extra || {});
  if (!mats.has(key)) mats.set(key, new THREE.MeshLambertMaterial({color, flatShading: true, ...extra}));
  return mats.get(key);
}
function box(parent, w, h, d, color, x, y, z, opt = {}) {
  const m = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), opt.material || mat(color));
  m.position.set(x, y + h / 2, z); m.castShadow = opt.shadow !== false; m.receiveShadow = true;
  parent.add(m); return m;
}

/* ---------------- the office: floor, walls, zones and the layout's props ---------------- */
function buildOffice(scene, L, S, colors, ctx) {
  const g = new THREE.Group(); scene.add(g);
  const th = L.theme || {};
  const floor = new THREE.Mesh(new THREE.PlaneGeometry(L.cols, L.rows), new THREE.MeshLambertMaterial({color: th.floor || "#b9bec6"}));
  floor.rotation.x = -Math.PI / 2; floor.position.set(L.cols / 2, 0, L.rows / 2); floor.receiveShadow = true; g.add(floor);
  // back wall (the two wall rows of the layout) with a skirting line
  const wall = new THREE.Color(th.wall || "#c96f53");
  box(g, L.cols, WALL_H, 0.5, null, L.cols / 2, 0, 1.75, {material: mat("#" + wall.getHexString())});
  // its top edge: a little higher and wider than the wall, so no face lies in the wall's plane (that flickers)
  box(g, L.cols + 0.02, 0.16, 0.52, null, L.cols / 2, WALL_H - 0.15, 1.75, {shadow: false, material: mat("#" + wall.clone().multiplyScalar(0.68).getHexString())});
  // zones: a tinted wooden floor with a coloured edge per department (or per HQ)
  S.zones.forEach((z, i) => {
    const col = new THREE.Color(colors[i % colors.length]);
    const wood = new THREE.Color(z.down ? "#8d9097" : "#e2c69e").lerp(col, z.down ? 0.05 : 0.18);
    const p = new THREE.Mesh(new THREE.PlaneGeometry(z.w, z.h), new THREE.MeshLambertMaterial({color: wood}));
    p.rotation.x = -Math.PI / 2; p.position.set(z.x + z.w / 2, 0.01, z.y + z.h / 2); p.receiveShadow = true; g.add(p);
    const e = 0.12, edge = mat(colors[i % colors.length]);
    for (const [w, d, x, zz] of [[z.w, e, z.x + z.w / 2, z.y + e / 2], [z.w, e, z.x + z.w / 2, z.y + z.h - e / 2],
      [e, z.h, z.x + e / 2, z.y + z.h / 2], [e, z.h, z.x + z.w - e / 2, z.y + z.h / 2]])
      box(g, w, 0.04, d, null, x, 0.01, zz, {material: edge, shadow: false});
  });
  // each prop in its own group: built around its centre in its own frame, then turned and put in place
  const screens = new Map(), groups = [];
  for (const p of L.props) {
    const e = entry(ctx.cat, p.id), q = new THREE.Group();
    q.position.set(p.x + p.w / 2, 0, p.y + p.h / 2); q.rotation.y = -rad(p.rot);
    q.userData.pick = p.parent || p.uid; q.userData.marker = !!e.marker;  // a marker (a waypoint) shows in Edit mode only
    buildProp(q, p, e, ctx, screens);
    g.add(q); groups.push(q);
  }
  return {group: g, screens, groups};
}
function buildProp(q, p, e, ctx, screens) {
  const w = p.w0, h = p.h0;
  const add = (name, x, y, z, rot = 0) => {const o = M.core[name].clone(); o.position.set(x, y, z); o.rotation.y = rot; q.add(o); return o};
  if (e.glb) {
    const m = propModel(ctx, e);
    if (!m) return placeholder(q, w, h, e.color);
    const o = m.clone(); o.rotation.y = -rad(e.rot || 0); o.position.y = e.y || 0; q.add(o); return;
  }
  switch (e.builtin) {
    case "desk": {
      add("desk", 0, 0, 0); const top = M.core.desk.userData.size.y;
      add("computerScreen", 0, top, -0.35);
      add("computerKeyboard", 0, top, 0.3);
      add("computerMouse", 0.75, top, 0.3);
      // the screen's glow, on the panel: code scrolls on it while the agent at this desk works
      const pn = M.core.computerScreen.userData.panel;
      const glow = new THREE.Mesh(new THREE.PlaneGeometry(pn.w, pn.h), new THREE.MeshBasicMaterial({color: "#1d2533"}));
      glow.position.set(pn.centre.x, top + pn.centre.y, -0.35 + pn.centre.z).addScaledVector(pn.normal, 0.004);
      glow.quaternion.setFromUnitVectors(new THREE.Vector3(0, 0, 1), pn.normal); q.add(glow);  // facing out of the screen
      glow.userData = {tex: codeTexture(), speed: 0.05, next: 0, blink: 0, last: 0};
      for (const a of p.anchors) screens.set(a.name, glow);
      break;
    }
    case "chair": add("chairDesk", 0, 0, -0.3, Math.PI); break;  // back to the south: the agent faces the desk
    case "bookshelf": bookshelf(q, 0, 0, w); break;
    case "whiteboard": whiteboard(q, 0, 0); break;
    case "coffee_bar": box(q, w, 1.05, 1.1, "#6b4f3a", 0, 0, -h / 2 + 0.6); box(q, w + 0.1, 0.08, 1.2, "#e8e2d6", 0, 1.05, -h / 2 + 0.6); break;
    case "espresso": box(q, 0.7, 0.55, 0.5, "#3b3f46", 0, 1.13, -h / 2 + 0.5); box(q, 0.5, 0.12, 0.3, "#c0c6cf", 0, 1.68, -h / 2 + 0.5); break;
    case "fridge": box(q, 0.8, 1.1, 0.8, "#eef1f4", 0, 0, -h / 2 + 0.5); box(q, 0.05, 0.4, 0.05, "#9aa1aa", 0.28, 0.55, -h / 2 + 0.92); break;
    case "coffee_table": box(q, w, 0.08, h, "#a8774f", 0, 0.42, 0); for (const [dx, dz] of [[-1, -1], [1, -1], [-1, 1], [1, 1]])
      box(q, 0.08, 0.42, 0.08, "#5d4330", dx * (w / 2 - 0.12), 0, dz * (h / 2 - 0.12)); break;
    case "beanbag": {
      const m = new THREE.Mesh(new THREE.IcosahedronGeometry(0.7, 1), mat(e.color || "#4a5568"));
      m.scale.set(1, 0.55, 1); m.position.set(0, 0.38, 0); m.castShadow = m.receiveShadow = true; q.add(m); break;
    }
    case "rug": {
      const r = new THREE.Mesh(new THREE.PlaneGeometry(w, h), new THREE.MeshLambertMaterial({color: e.color || "#5a7d9a"}));
      r.rotation.x = -Math.PI / 2; r.position.set(0, 0.02, 0); r.receiveShadow = true; q.add(r); break;
    }
    case "window": {
      const z = h / 2;
      box(q, w, 1.5, 0.08, "#f2f2f2", 0, 0.75, z + 0.02, {shadow: false});
      box(q, w - 0.25, 1.3, 0.09, null, 0, 0.85, z + 0.03, {shadow: false, material: glass});
      box(q, 0.08, 1.3, 0.1, "#f2f2f2", 0, 0.85, z + 0.04, {shadow: false}); break;
    }
    case "door": {  // a doorway: a frame to walk through (it does not block)
      const c = e.color || "#c9ced6", d = Math.max(0.12, h);
      for (const dx of [-1, 1]) box(q, 0.14, 2.2, d, c, dx * (w / 2 - 0.07), 0, 0);
      box(q, w, 0.16, d, c, 0, 2.2, 0); break;
    }
    case "waypoint": {  // a ring on the floor with a little flag
      const ring = new THREE.Mesh(new THREE.RingGeometry(0.28, 0.4, 24), new THREE.MeshBasicMaterial({color: "#f6e05e", side: THREE.DoubleSide}));
      ring.rotation.x = -Math.PI / 2; ring.position.y = 0.05; q.add(ring);
      box(q, 0.05, 0.9, 0.05, "#f6e05e", 0, 0, 0, {shadow: false}); box(q, 0.3, 0.18, 0.03, "#f6ad55", 0.15, 0.7, 0, {shadow: false}); break;
    }
    case "partition": box(q, w, 1.5, Math.max(0.12, h), e.color || "#d9dde3", 0, 0, 0); box(q, w + 0.02, 0.06, Math.max(0.14, h) + 0.02, "#8a94a3", 0, 1.5, 0, {shadow: false}); break;
    case "sofa": {
      const c = e.color || "#4f6d8f";
      box(q, w, 0.42, h, c, 0, 0, 0.05); box(q, w, 0.55, 0.3, c, 0, 0.42, -h / 2 + 0.15);
      for (const dx of [-1, 1]) box(q, 0.25, 0.3, h, c, dx * (w / 2 - 0.125), 0.42, 0.05);
      box(q, w - 0.6, 0.12, h - 0.4, "#6d8fb3", 0, 0.42, 0.15, {shadow: false}); break;
    }
    case "meeting_table": {
      box(q, w, 0.08, h, "#e8e2d6", 0, 0.72, 0);
      for (const [dx, dz] of [[-1, -1], [1, -1], [-1, 1], [1, 1]]) box(q, 0.1, 0.72, 0.1, "#5d6470", dx * (w / 2 - 0.25), 0, dz * (h / 2 - 0.25));
      break;
    }
    case "floor_lamp": {
      box(q, 0.4, 0.05, 0.4, "#3b3f46", 0, 0, 0); box(q, 0.06, 1.6, 0.06, "#3b3f46", 0, 0.05, 0);
      const shadeMesh = new THREE.Mesh(new THREE.CylinderGeometry(0.22, 0.32, 0.35, 10), mat("#fbe7b5", {emissive: "#7a5d1f"}));
      shadeMesh.position.set(0, 1.75, 0); q.add(shadeMesh); break;
    }
    case "server_rack": {
      box(q, w * 0.9, 2.0, h * 0.9, "#23272f", 0, 0, 0);
      for (let i = 0; i < 6; i++) box(q, w * 0.7, 0.04, 0.02, i % 2 ? "#4fd1c5" : "#68d391", 0, 0.3 + i * 0.27, h * 0.45 + 0.01, {shadow: false, material: mat(i % 2 ? "#4fd1c5" : "#68d391", {emissive: i % 2 ? "#1d6f69" : "#2f6f45"})});
      break;
    }
    default: placeholder(q, w, h, e.color);
  }
}
// a prop whose model is missing (or did not load): a box of its size, so it can still be seen, moved and removed
function placeholder(q, w, h, color) {
  box(q, Math.max(0.2, w - 0.1), 0.6, Math.max(0.2, h - 0.1), null, 0, 0, 0, {material: mat(color || "#d53f8c", {transparent: true, opacity: 0.6})});
}
function bookshelf(g, cx, cz, w) {
  const z = cz - 0.25, BOOKS = ["#c53030", "#2b6cb0", "#2f855a", "#d69e2e", "#6b46c1", "#dd6b20"];
  box(g, w, 2.1, 0.6, "#8b5e3c", cx, 0, z);
  for (let s = 0; s < 3; s++) {
    let x = cx - w / 2 + 0.15;
    for (let k = 0; x < cx + w / 2 - 0.25; k++) {
      const bw = 0.12 + ((k * 7 + s * 3) % 4) * 0.04, bh = 0.38 + ((k * 5 + s) % 3) * 0.06;
      box(g, bw, bh, 0.45, BOOKS[(k + s * 2) % BOOKS.length], x + bw / 2, 0.15 + s * 0.65, z + 0.1, {shadow: false}); x += bw + 0.02;
    }
  }
}
function whiteboard(g, cx, cz) {
  const z = cz - 0.2;
  box(g, 1.9, 1.15, 0.06, "#f7f7f2", cx, 0.75, z);
  box(g, 2.0, 0.07, 0.1, "#7a5a3c", cx, 1.9, z); box(g, 2.0, 0.07, 0.12, "#7a5a3c", cx, 0.72, z);
  for (const dx of [-0.9, 0.9]) box(g, 0.07, 1.95, 0.07, "#7a5a3c", cx + dx, 0, z);
  for (const [w, y, c] of [[1.1, 1.55, "#3182ce"], [0.7, 1.35, "#e53e3e"], [0.9, 1.15, "#38a169"]]) box(g, w, 0.04, 0.01, c, cx - 0.3 + w / 2 - 0.4, y, z + 0.04, {shadow: false});
}

/* ---------------- mount ---------------- */
/* ctx: {el, L, S, colors, statuses, stateOf, zoneLinks, onPick(id), onZone(zone),
         cat: the catalog, characters: default character ids, asset(name): url of an uploaded file, view: a camera to keep} */
export async function mount(ctx) {
  const {el, S, colors} = ctx;
  let L = ctx.L;
  await prepare(ctx, L);
  const renderer = new THREE.WebGLRenderer({antialias: true});  // throws without WebGL: office.js then falls back to 2D
  renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
  renderer.shadowMap.enabled = true; renderer.shadowMap.type = THREE.PCFShadowMap;
  el.innerHTML = ""; el.appendChild(renderer.domElement);
  const overlay = document.createElement("div"); overlay.className = "o3"; el.appendChild(overlay);
  const scene = new THREE.Scene(); scene.background = new THREE.Color((L.theme || {}).background || "#12151b");
  scene.add(new THREE.HemisphereLight("#ffffff", "#5a6378", 1.7));
  const sun = new THREE.DirectionalLight("#fff4e0", 2.4);
  scene.add(sun, sun.target);
  function light() {
    const span = Math.max(L.cols, L.rows);
    sun.position.set(L.cols * 0.35, span * 1.1, L.rows * 1.1); sun.target.position.set(L.cols / 2, 0, L.rows / 2);
    sun.castShadow = true; sun.shadow.mapSize.set(2048, 2048); sun.shadow.bias = -0.0005; sun.shadow.normalBias = 0.03;
    Object.assign(sun.shadow.camera, {left: -span, right: span, top: span, bottom: -span, near: 1, far: span * 4});
    sun.shadow.camera.updateProjectionMatrix();
  }
  light();
  let office = buildOffice(scene, L, S, colors, ctx);
  let G, anchors, spots, roam, D = null;  // D: what idle agents do (officeidle.js)
  function walkways() {  // where agents can go: the walkable tiles, and the spots (anchors) of the layout's props, waypoints too
    G = walkGrid(L); anchors = {}; spots = {rest: [], social: [], utility: [], wander: []};
    for (const p of L.props) for (const a of p.anchors || []) {
      const t = {c: Math.round(p.x + a.ox), r: Math.round(p.y + a.oy)};
      anchors[a.name] = {...t, rot: a.rot || 0}; if (spots[a.type]) spots[a.type].push({...nearest(G, t), rot: a.rot || 0});
    }
    roam = [...spots.wander, ...spots.social, ...spots.utility];
    const idleSpots = [];  // spots with an act: read at a shelf, sit on the sofa, ...; the agent stands on the tile in front
    for (const p of L.props) for (const a of p.anchors || []) if (a.act) {
      const t = {c: Math.round(p.x + a.ox), r: Math.round(p.y + a.oy)}, tile = nearest(G, t), cx = t.c + 0.5, cz = t.r + 0.5;
      let seat = {x: cx, z: cz};  // where a sitting agent is drawn: on the prop (the sofa's long axis through the front tile)
      if (a.act === "sit" && a.sx !== undefined) seat = {x: a.sx, z: a.sy};  // its own place on the cushion
      else if (a.act === "sit" && a.fx !== undefined) {
        const horiz = (a.rot || 0) % 180 === 0;
        seat = horiz ? {x: cx, z: a.fy} : {x: a.fx, z: cz};
      }
      idleSpots.push({name: a.name, act: a.act, prop: a.prop, tile, seat, rot: a.rot || 0, fx: a.fx, fy: a.fy});
    }
    D = new IdleDirector(idleSpots, ctx.idle || {}, Math.random);
  }
  walkways();
  const showMarkers = () => {for (const q of office.groups) if (q.userData.marker) q.visible = !!editing};
  // a seat turns with its desk (or beanbag): which way the agent faces there, and where exactly it sits
  const facing = (base, rot) => base - rad(rot || 0);
  const perchAt = (p, rot) => {const a = rad(rot || 0); return {y: p.y, x: -p.z * Math.sin(a), z: p.z * Math.cos(a)}};

  // camera: looking at the office from the front and above; drag to rotate, wheel to zoom, right-drag to pan
  const camera = new THREE.PerspectiveCamera(38, 1, 0.5, 600);
  const controls = new OrbitControls(camera, renderer.domElement);
  Object.assign(controls, {enableDamping: true, dampingFactor: 0.12, maxPolarAngle: 1.32, minDistance: 4, maxDistance: Math.max(L.cols, L.rows) * 4, screenSpacePanning: false});
  let zoom = "fit";
  // the starting view: from the front-left corner, a little above, so the whole office reads at a glance
  const VIEW_AZ = 0.74, VIEW_EL = 0.52;  // radians: turned left from straight ahead, raised from the floor
  function frame(z) {
    const w = el.clientWidth || 1, h = el.clientHeight || 1; camera.aspect = w / h; camera.updateProjectionMatrix();
    const center = new THREE.Vector3(L.cols / 2, 0, L.rows / 2);
    const dir = new THREE.Vector3(-Math.sin(VIEW_AZ) * Math.cos(VIEW_EL), Math.sin(VIEW_EL), Math.cos(VIEW_AZ) * Math.cos(VIEW_EL));
    const corners = [];
    for (const x of [0, L.cols]) for (const y of [0, 3]) for (const zz of [0, L.rows]) corners.push(new THREE.Vector3(x, y, zz));
    const fits = d => {  // every corner of the office inside the view, with a margin
      camera.position.copy(center).addScaledVector(dir, d); camera.lookAt(center); camera.updateMatrixWorld();
      return corners.every(c => {const q = c.clone().project(camera); return Math.abs(q.x) < 0.96 && Math.abs(q.y) < 0.94 && q.z < 1})};
    let lo = 2, hi = Math.max(L.cols, L.rows) * 4;
    for (let i = 0; i < 30; i++) {const mid = (lo + hi) / 2; if (fits(mid)) hi = mid; else lo = mid}
    let d = hi;
    if (z === "1") d *= 0.55; else if (z === "2") d *= 0.32;
    controls.target.copy(center); camera.position.copy(center).addScaledVector(dir, d); controls.update();
  }
  function resize() {
    const w = el.clientWidth || 1, h = el.clientHeight || 1;
    renderer.setSize(w, h, false); renderer.domElement.style.width = "100%"; renderer.domElement.style.height = "100%";
    camera.aspect = w / h; camera.updateProjectionMatrix();
  }
  let moved = false;  // once the user moves the camera, a resize keeps their view; until then it re-fits
  const ro = new ResizeObserver(() => {resize(); if (!moved) frame(zoom)}); ro.observe(el);
  resize(); frame(zoom);
  if (ctx.view) {camera.position.fromArray(ctx.view.pos); controls.target.fromArray(ctx.view.target); controls.update(); moved = true}

  // zone tags: HTML on top of the canvas; in the planners room they open that HQ's office
  const zoneTags = S.zones.map((z, i) => {
    const d = document.createElement(ctx.zoneLinks ? "a" : "div"); d.className = "o3-zone" + (ctx.zoneLinks ? " link" : "");
    d.style.background = colors[i % colors.length]; d.textContent = z.label + (z.down ? " · offline" : "");
    if (ctx.zoneLinks) {d.href = "#"; d.onclick = e => {e.preventDefault(); ctx.onZone(z)}}
    overlay.appendChild(d);  // a sign on the back wall for the first row of zones, on the floor for the others
    return {d, z};
  });
  const tagAt = z => z.y <= 3 ? new THREE.Vector3(z.x + 0.3, WALL_H - 0.35, 2.05) : new THREE.Vector3(z.x + 0.2, 0.05, z.y + 0.1);

  /* ---------------- characters ---------------- */
  const pickables = [], A = new Map();
  const ring = new THREE.Mesh(new THREE.RingGeometry(0.42, 0.58, 32), new THREE.MeshBasicMaterial({color: "#7aa2ff", side: THREE.DoubleSide}));
  ring.rotation.x = -Math.PI / 2; ring.visible = false; scene.add(ring);
  const trail = new THREE.Line(new THREE.BufferGeometry(), new THREE.LineBasicMaterial({color: "#7aa2ff"})); trail.visible = false; scene.add(trail);
  let order = 0;
  // the tiles agents head for (one agent each), and their desk seats (nobody else stops there)
  const claims = new Map(), seats = new Map(), key = t => `${t.c},${t.r}`;
  for (const c of S.cit.values()) {
    const want = (L.characters || {})[c.id], def = ctx.characters[c.look % ctx.characters.length];
    const m = M.chars.get(want) || M.chars.get(def) || [...M.chars.values()][0];
    if (!m) break;
    const obj = cloneSkinned(m.scene);
    obj.scale.setScalar(m.scale);
    const materials = [];
    obj.traverse(o => {if (o.isMesh) {o.material = o.material.clone(); materials.push(o.material); o.castShadow = true; o.frustumCulled = false}});
    const grp = new THREE.Group(); grp.add(obj); scene.add(grp);
    const hit = new THREE.Mesh(new THREE.CylinderGeometry(0.5, 0.5, CHAR_H, 8), new THREE.MeshBasicMaterial({visible: false}));
    hit.position.y = CHAR_H / 2; hit.userData.id = c.id; grp.add(hit); pickables.push(hit);
    const mixer = new THREE.AnimationMixer(obj), actions = {};
    for (const [n, clip] of Object.entries(m.clips)) actions[n] = mixer.clipAction(clip);
    const label = document.createElement("div"); label.className = "o3-label"; label.textContent = c.name; overlay.appendChild(label);
    if (ctx.zoneColor) label.style.borderColor = ctx.zoneColor(c);  // its department's colour
    const badge = document.createElement("div"); badge.className = "o3-badge"; overlay.appendChild(badge);
    const bub = document.createElement("div"); bub.className = "o3-bubble"; overlay.appendChild(bub);
    const icon = document.createElement("div"); icon.className = "o3-icon"; overlay.appendChild(icon);  // 📖 ☕ 💬 ... while it pretends
    const home = anchors[c.home] || {c: 2, r: 3, rot: 0}, seat = {...nearest(G, home), rot: home.rot};
    const a = {id: c.id, grp, mixer, actions, cur: null, materials, label, badge, bub, icon, iconTxt: "", bubUntil: 0, seat, tile: {c: seat.c, r: seat.r},
      x: seat.c + 0.5, z: seat.r + 0.5, path: [], goal: {c: seat.c, r: seat.r}, pose: "sit", face: facing(Math.PI, seat.rot), perch: perchAt(SEAT, seat.rot),
      retry: 0, plan: null, visit: null, order: order++, dim: 1, want: key(seat), claim: key(seat), off: {x: 0, z: 0}, look: 0, wait: 0, stuck: 0};
    seats.set(key(seat), c.id); claims.set(key(seat), c.id);
    grp.position.set(a.x + a.perch.x, a.perch.y, a.z + a.perch.z); grp.rotation.y = a.face;
    A.set(c.id, a); play(a, "sit", 0);
  }
  function play(a, name, fade = 0.25) {
    const next = a.actions[name] || a.actions.idle; if (!next || a.cur === next) return;
    next.reset().setEffectiveWeight(1).fadeIn(fade).play(); if (a.cur) a.cur.fadeOut(fade); a.cur = next;
  }
  // the tile an agent heads for: t, or the nearest walkable one that no other agent heads for (or sits at)
  function claim(a, t) {
    if (a.claim && claims.get(a.claim) === a.id) claims.delete(a.claim);
    const g = nearest(G, t, (c, r) => {const k = `${c},${r}`, o = claims.get(k), s = seats.get(k); return (!o || o === a.id) && (!s || s === a.id)});
    a.claim = key(g); claims.set(a.claim, a.id);
    return g;
  }
  const taken = (a, t) => {const o = claims.get(key(t)); return !!o && o !== a.id};
  // walk to tile t, then take a pose there; perch: where to sit relative to the tile (a chair, a beanbag). When another
  // agent has the tile, it goes next to it and stands
  function go(a, t, pose, face, perch = null, now = Date.now()) {
    if (a.want !== key(t)) {a.want = key(t); a.goal = claim(a, t); a.path = []; a.retry = 0}
    const there = a.goal.c === t.c && a.goal.r === t.r;
    a.pose = there || pose !== "sit" ? pose : "idle"; a.face = face; a.perch = there ? perch : null;
    if (a.path.length || now < a.retry || at(a, a.goal)) return;
    const p = findPath(G, a.tile, a.goal);
    a.path = p ? smooth(G, a.tile, p) : []; a.retry = p ? 0 : now + 3000;  // unreachable: stay put and try again later
  }
  const at = (a, t) => a.tile.c === t.c && a.tile.r === t.r;
  // an agent standing (or sitting) in the way: walk around it, or wait a moment; after a few seconds, walk on through
  function ahead(a, now) {
    if (now < a.look) return;
    a.look = now + 250;
    const n = a.path[0], dx = n.c + 0.5 - a.x, dz = n.r + 0.5 - a.z, d = Math.hypot(dx, dz) || 1, k = Math.min(0.8, d) / d;
    const t = {c: Math.floor(a.x + dx * k), r: Math.floor(a.z + dz * k)};
    if (at(a, t) || key(t) === key(a.goal)) return;
    const still = [...A.values()].filter(b => b !== a && !b.path.length && b.grp.visible);
    if (!still.some(b => at(b, t))) {a.stuck = 0; return}
    if (!a.stuck) a.stuck = now; else if (now - a.stuck > 4000) return;
    const avoid = new Set(still.map(b => key(b.tile))); avoid.delete(key(a.tile));
    const p = findPath(G, a.tile, a.goal, avoid);
    if (p) a.path = smooth(G, a.tile, p, avoid); else a.wait = now + 700;
  }
  // walkers that come close step a little aside, each to its right (drawn only: they keep to their paths)
  function spread(dt) {
    for (const a of A.values()) {
      if (a.path.length) {
        const n = a.path[0], mx = n.c + 0.5 - a.x, mz = n.r + 0.5 - a.z, m = Math.hypot(mx, mz) || 1, px = -mz / m, pz = mx / m;
        for (const b of A.values()) {
          if (b === a || !b.grp.visible) continue;
          const dx = a.x - b.x, dz = a.z - b.z, d = Math.hypot(dx, dz);
          if (d >= 0.8) continue;
          const side = px * dx + pz * dz, sgn = Math.abs(side) < 0.05 ? 1 : Math.sign(side), f = (0.8 - d) * dt * 6;
          a.off.x += px * sgn * f; a.off.z += pz * sgn * f;
        }
      }
      const k = Math.max(0, 1 - dt * 2.5), l = Math.hypot(a.off.x, a.off.z), cap = l > 0.35 ? 0.35 / l : 1;
      a.off.x *= k * cap; a.off.z *= k * cap;
    }
  }
  const sitAtDesk = (a, now) => go(a, a.seat, "sit", facing(Math.PI, a.seat.rot), perchAt(SEAT, a.seat.rot), now);
  // what each agent does right now, from the state office.js gives it
  function decide(a, now) {
    const c = S.cit.get(a.id); if (!c) return;
    if (a.cheer && now < a.cheer.until) {  // a task just finished: stand up where it is and nod (or shake its head)
      a.path = []; a.goal = {...a.tile}; a.want = null; a.pose = a.cheer.ok ? "emote-yes" : "emote-no"; a.perch = null; a.iconTxt = ""; if (D) D.release(a.id); return;
    }
    a.cheer = null;
    const st = ctx.stateOf(c);
    if (D && (st !== "idle" || a.visit)) {D.release(a.id); a.iconTxt = ""}  // work (or a visit) comes first: the spot is free again
    if (a.visit && (now > a.visit.until || st === "offline")) a.visit = null;
    if (a.visit) {  // walk over to the agent it messages, talk for a few seconds, then carry on
      const b = A.get(a.visit.to); if (!b) {a.visit = null; return}
      if (Math.hypot(a.x - b.x, a.z - b.z) < 1.8 && !a.path.length) {
        if (!a.visit.talk) a.visit.talk = now + 3500;
        a.pose = "emote-yes"; a.face = Math.atan2(b.x - a.x, b.z - a.z); a.perch = null;
        if (now > a.visit.talk) a.visit = null;
        return;
      }
      const g = a.goal;
      if (!g || Math.max(Math.abs(g.c - b.tile.c), Math.abs(g.r - b.tile.r)) !== 1) {  // a free tile next to it, on this side
        let t = null, best = Infinity;
        for (let dr = -1; dr <= 1; dr++) for (let dc = -1; dc <= 1; dc++) {
          const c = b.tile.c + dc, r = b.tile.r + dr, d = Math.hypot(c + 0.5 - a.x, r + 0.5 - a.z);
          if ((dc || dr) && G.ok(c, r) && !taken(a, {c, r}) && d < best) {t = {c, r}; best = d}
        }
        go(a, t || {c: b.tile.c + (a.x <= b.x ? -1 : 1), r: b.tile.r + 1}, "idle", 0, null, now);
      }
      return;
    }
    if (ctx.recalled && ctx.recalled(c) && (st === "idle" || st === "sleeping")) {  // its monitor was clicked: back to the seat
      if (D) D.release(a.id); a.iconTxt = ""; a.plan = null; return sitAtDesk(a, now);
    }
    if (st === "sleeping") {
      const r = spots.rest.map((_, i) => spots.rest[(a.order + i) % spots.rest.length]).find(r => !taken(a, r));  // a free beanbag
      if (!r) return sitAtDesk(a, now);
      return go(a, r, "sit", facing(0, r.rot), perchAt(REST, r.rot), now);
    }
    if (st === "idle") {
      const P = D && D.enabled ? D.current(a.id, now) || D.plan(a.id, {idle: idleIds(), total: A.size,
        free: s => !taken(a, s.tile) && (at(a, s.tile) || !!findPath(G, a.tile, s.tile))}, now) : null;
      if (P) return pretend(a, P, now);
      a.iconTxt = "";
      if (!a.plan || now > a.plan.until) {
        const open = roam.filter(s => !taken(a, s));
        a.plan = Math.random() < 0.45 && open.length ? {spot: open[Math.floor(Math.random() * open.length)], until: now + rand(6000, 14000), face: rand(-1, 1)}
          : {spot: a.seat, until: now + rand(8000, 20000)};
      }
      const s = a.plan.spot;
      return s === a.seat ? sitAtDesk(a, now) : go(a, s, "idle", a.plan.face, null, now);
    }
    a.plan = null;
    sitAtDesk(a, now);  // working, waiting for you, offline, called back by a click on its monitor: at the desk
  }
  const idleIds = () => [...A.keys()].filter(id => S.cit.get(id) && ctx.stateOf(S.cit.get(id)) === "idle");
  // walk to the plan's spot and act it out: stand and face the prop (read, drink, write, tend), sit on the sofa, or chat
  function pretend(a, P, now) {
    const s = P.spot, kind = P.act === "chat" ? P.base : P.act, def = IDLE_ACTS[kind];
    const tx = s.tile.c + 0.5, tz = s.tile.r + 0.5;
    let pose = def.pose, face = Math.atan2(s.fx - tx, s.fy - tz) || 0, perch = null;
    if (kind === "sit") {face = facing(0, s.rot); perch = {y: SOFA_Y, x: s.seat.x - tx, z: s.seat.z - tz}}
    if (P.act === "chat") {  // face the others; standing chatters nod in turns
      const mates = (P.crew || []).filter(m => m !== a.id).map(m => D.plans.get(m)).filter(Boolean);
      if (mates.length && kind !== "sit") {
        const mx = mates.reduce((t, m) => t + m.spot.tile.c + 0.5, 0) / mates.length, mz = mates.reduce((t, m) => t + m.spot.tile.r + 0.5, 0) / mates.length;
        if (Math.hypot(mx - tx, mz - tz) > 0.1) face = Math.atan2(mx - tx, mz - tz);
      }
      if (kind !== "sit") pose = Math.floor(now / 2600 + a.order) % 2 ? CHAT.pose : "idle";
    }
    go(a, s.tile, pose, face, perch, now);
    const here = !a.path.length && a.goal && at(a, a.goal) && a.goal.c === s.tile.c && a.goal.r === s.tile.r;
    a.iconTxt = here ? (P.act === "chat" ? CHAT.icon : def.icon) : "";
    if (here) D.arrive(a.id, now);  // the stay counts from arrival
  }
  function step(a, dt, now) {
    const fast = a.hurry > now, s = SPEED * dt * (fast ? 2.2 : 1);  // a new task: it runs to its desk
    if (a.path.length) ahead(a, now);
    if (a.path.length && now < a.wait) play(a, "idle", 0.2);  // someone is in the way
    else if (a.path.length) {
      const n = a.path[0], tx = n.c + 0.5, tz = n.r + 0.5, dx = tx - a.x, dz = tz - a.z, d = Math.hypot(dx, dz);
      if (d <= s) {a.x = tx; a.z = tz; a.tile = n; a.path.shift()}
      else {a.x += dx / d * s; a.z += dz / d * s; const c = Math.floor(a.x), r = Math.floor(a.z); if (G.ok(c, r)) a.tile = {c, r}}  // a straight line crosses tiles
      turnTo(a, Math.atan2(dx, dz), dt); play(a, fast ? "sprint" : "walk", 0.15);
    } else {
      const dx = a.tile.c + 0.5 - a.x, dz = a.tile.r + 0.5 - a.z, d = Math.hypot(dx, dz);  // finish the last step
      if (d > 0.01) {const k = Math.min(1, s / d); a.x += dx * k; a.z += dz * k; turnTo(a, Math.atan2(dx, dz), dt); play(a, "walk", 0.15)}
      else {const there = a.goal && at(a, a.goal); turnTo(a, a.face, dt); play(a, there ? a.pose : "idle")}
    }
    const perch = !a.path.length && a.pose === "sit" && a.goal && at(a, a.goal) && a.perch;
    a.grp.position.set(a.x + (perch ? perch.x || 0 : a.off.x), perch ? perch.y : 0, a.z + (perch ? perch.z : a.off.z));
  }
  function turnTo(a, to, dt) {
    let r = a.grp.rotation.y, d = ((to - r + Math.PI) % (2 * Math.PI) + 2 * Math.PI) % (2 * Math.PI) - Math.PI;
    a.grp.rotation.y = r + d * Math.min(1, dt * 10);
  }

  /* ---------------- overlay: names, "needs you" badges, speech bubbles ---------------- */
  const v = new THREE.Vector3();
  function project(p) {
    v.copy(p).project(camera);
    return {x: (v.x + 1) / 2 * el.clientWidth, y: (1 - v.y) / 2 * el.clientHeight, ok: v.z < 1 && Math.abs(v.x) < 1.2 && Math.abs(v.y) < 1.2};
  }
  const place = (d, p, show = true) => {const q = show && project(p); d.style.display = q && q.ok ? "" : "none"; if (q && q.ok) d.style.transform = `translate(${q.x}px,${q.y}px)`};
  function paint(now) {
    for (const [id, a] of A) {
      const c = S.cit.get(id); if (!c) continue;
      if (editing) {a.label.style.display = a.badge.style.display = a.bub.style.display = "none"; continue}
      const st = ctx.stateOf(c), head = new THREE.Vector3(a.grp.position.x, a.grp.position.y ? CHAR_H * 0.95 : CHAR_H + 0.1, a.grp.position.z);
      a.label.classList.toggle("sel", S.sel === id); a.label.classList.toggle("dim", st === "offline");
      place(a.label, head);
      const done = ctx.doneOf ? ctx.doneOf(c) : [], ok = done.every(x => x.ok);  // finished tasks you have not looked at
      const b = c.att.length ? (c.att.length > 1 ? String(c.att.length) : "!") : st === "error" ? "⚠"
        : done.length ? (ok ? "✓" : "✗") + (done.length > 1 ? " " + done.length : "") : st === "sleeping" ? "z z" : "";
      a.badge.textContent = b;
      a.badge.className = "o3-badge" + (c.att.length || st === "error" ? "" : done.length ? (ok ? " ok" : " fail") : st === "sleeping" ? " zz" : "")
        + (c.att.length || done.length ? " blink" : "");  // blinks until answered / dismissed (alerts.py)
      place(a.badge, head.clone().setY(head.y + 0.55), !!b);
      a.icon.textContent = a.iconTxt || ""; place(a.icon, head.clone().setY(head.y + 0.5), !!a.iconTxt && !b);
      if (a.bubUntil && now > a.bubUntil) {a.bubUntil = 0}
      place(a.bub, head.clone().setY(head.y + (b ? 1.1 : 0.55)), !!a.bubUntil);
      const dim = st === "offline" ? 0.22 : 1;
      if (dim !== a.dim) {a.dim = dim; for (const m of a.materials) {m.transparent = dim < 1; m.opacity = dim; m.depthWrite = dim === 1}}
      const glow = office.screens.get(c.home);
      if (glow) screen(glow, st, now, st !== "offline" && ctx.alertOf ? ctx.alertOf(c) : null);
    }
    for (const t of zoneTags) place(t.d, tagAt(t.z));
    const s = !editing && S.sel && A.get(S.sel);
    ring.visible = !!s; if (s) ring.position.set(s.x, 0.04, s.z);
    trail.visible = false;  // the selected agent's walking path is not drawn
  }

  // a desk's screen: dark when idle; scrolling code (at a pace that changes now and then, with a flicker) when working
  const FLASH = {attention: "#ff5d6c", error: "#ff5d6c", auto_confirmed: "#f6ad55", done: "#38a169"};
  function screen(glow, st, now, need) {
    const m = glow.material, u = glow.userData;
    if (need) {  // something needs the user: the monitor flashes until it is answered / dismissed (alerts.py)
      if (m.map) {m.map = null; m.needsUpdate = true}
      m.color.set(Math.floor(now / 450) % 2 === 0 ? FLASH[need] || FLASH.attention : "#1d2533");
      u.last = now;
      return;
    }
    const on = st === "working" || st === "error";
    if (!!m.map !== on) {m.map = on ? u.tex : null; m.needsUpdate = true}
    if (st === "working") {
      if (now > u.next) {u.speed = Math.random() < 0.2 ? 0 : rand(0.03, 0.16); u.blink = Math.random() < 0.3 ? now + rand(60, 200) : 0; u.next = now + rand(700, 3200)}
      u.tex.offset.y = ((u.tex.offset.y - u.speed * Math.min(0.2, (now - (u.last || now)) / 1000)) % 1 + 1) % 1;  // new lines come in at the bottom
      m.color.setScalar(u.blink > now ? 0.4 : 1);
    } else m.color.set(st === "error" ? "#ff8a95" : "#1d2533");
    u.last = now;
  }

  /* ---------------- double-click an agent: the camera follows it (drag still turns, wheel zooms) ---------------- */
  let follow = null;
  function track(dt) {
    if (!follow) return;
    const a = A.get(follow);
    if (!a || editing) {follow = null; return}
    const k = Math.min(1, dt * 5), dx = (a.grp.position.x - controls.target.x) * k, dz = (a.grp.position.z - controls.target.z) * k;
    controls.target.x += dx; controls.target.z += dz; camera.position.x += dx; camera.position.z += dz;
    const off = camera.position.clone().sub(controls.target), d = off.length();
    if (a.closer && d > 13) camera.position.copy(controls.target).addScaledVector(off, (d - (d - 12) * k) / d); else a.closer = false;
  }

  /* ---------------- Edit mode: the agents step out, props can be picked, dragged and changed ---------------- */
  let editing = null, selUid = null, drag = null, gridLines = null, tool = null, stroke = null, walk = null;
  showMarkers();
  const selBox = new THREE.Box3Helper(new THREE.Box3(), 0xffd166); selBox.visible = false; scene.add(selBox);
  const selFoot = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), new THREE.MeshBasicMaterial({color: "#ffd166", transparent: true, opacity: 0.35, depthWrite: false}));
  selFoot.rotation.x = -Math.PI / 2; selFoot.visible = false; scene.add(selFoot);  // the footprint, on the floor
  const floorPlane = new THREE.Plane(new THREE.Vector3(0, 1, 0), 0), hitPoint = new THREE.Vector3();
  function drawGrid() {
    if (gridLines) {scene.remove(gridLines); gridLines.geometry.dispose()}
    const pts = [];
    for (let c = 0; c <= L.cols; c++) pts.push(c, 0.03, 2, c, 0.03, L.rows);
    for (let r = 2; r <= L.rows; r++) pts.push(0, 0.03, r, L.cols, 0.03, r);
    const geo = new THREE.BufferGeometry(); geo.setAttribute("position", new THREE.Float32BufferAttribute(pts, 3));
    gridLines = new THREE.LineSegments(geo, new THREE.LineBasicMaterial({color: "#ffffff", transparent: true, opacity: 0.18}));
    gridLines.visible = !!editing; scene.add(gridLines);
  }
  drawGrid();
  // where agents can walk, on the floor: blocked tiles red (no-go paint darker), corridors green, the spots agents use
  // in the colour of their kind, and spots they cannot reach in magenta
  const SPOT = {work: [66, 153, 225], rest: [159, 122, 234], social: [237, 137, 54], utility: [56, 178, 172], wander: [246, 224, 94]};
  const PAINT = {block: [120, 20, 40, 190], path: [56, 161, 105, 140], erase: [0, 0, 0, 0]};
  function drawWalk() {
    if (walk) {scene.remove(walk.mesh); walk.mesh.geometry.dispose(); walk.tex.dispose()}
    const {cols, rows} = L, data = new Uint8Array(cols * rows * 4);
    const put = (c, r, rgba) => {if (c >= 0 && r >= 0 && c < cols && r < rows) data.set(rgba, ((rows - 1 - r) * cols + c) * 4)};
    for (let r = 0; r < rows; r++) for (let c = 0; c < cols; c++) {
      if (!G.ok(c, r)) put(c, r, L.paint.block.has(`${c},${r}`) ? PAINT.block : [229, 62, 62, 85]);
      else if (G.cost[G.at(c, r)] < 1) put(c, r, PAINT.path);
    }
    for (const p of L.props) for (const a of p.anchors) {const t = nearest(G, {c: Math.round(p.x + a.ox), r: Math.round(p.y + a.oy)}); put(t.c, t.r, [...(SPOT[a.type] || [255, 255, 255]), 200])}
    for (const u of unreachable(L, G)) put(u.c, u.r, [255, 0, 200, 235]);
    const tex = new THREE.DataTexture(data, cols, rows); tex.magFilter = THREE.NearestFilter; tex.needsUpdate = true;
    const mesh = new THREE.Mesh(new THREE.PlaneGeometry(cols, rows), new THREE.MeshBasicMaterial({map: tex, transparent: true, depthWrite: false}));
    mesh.rotation.x = -Math.PI / 2; mesh.position.set(cols / 2, 0.028, rows / 2); mesh.renderOrder = 1; mesh.visible = !!editing; scene.add(mesh);
    walk = {mesh, tex, put};
  }
  drawWalk();
  // a paint stroke: the tiles under the pointer (and between two moves), shown at once, kept on pointerup
  function brush(p) {
    const last = stroke.last || p, n = Math.max(1, Math.ceil(Math.hypot(p.x - last.x, p.z - last.z) / 0.25));
    for (let i = 1; i <= n; i++) {
      const c = Math.floor(last.x + (p.x - last.x) * i / n), r = Math.floor(last.z + (p.z - last.z) * i / n);
      if (c < 0 || r < 2 || c >= L.cols || r >= L.rows || stroke.tiles.has(`${c},${r}`)) continue;
      stroke.tiles.set(`${c},${r}`, [c, r]); walk.put(c, r, PAINT[tool] || PAINT.erase);
    }
    stroke.last = p; walk.tex.needsUpdate = true;
  }
  const groupsOf = uid => office.groups.filter(q => q.userData.pick === uid);
  function showSel() {
    const gs = selUid ? groupsOf(selUid) : [], p = selUid && L.byUid && L.byUid.get(selUid);
    selBox.visible = !!gs.length; selFoot.visible = !!(gs.length && p);
    if (gs.length) {selBox.box.makeEmpty(); for (const q of gs) selBox.box.expandByObject(q); selBox.box.min.y = 0}
    if (selFoot.visible) {
      selFoot.scale.set(p.w, p.h, 1);
      selFoot.position.set(p.x + p.w / 2 + (drag ? drag.dx : 0), 0.035, p.y + p.h / 2 + (drag ? drag.dz : 0));
    }
  }
  function propAt(e) {
    const r = cv.getBoundingClientRect();
    ndc.set((e.clientX - r.left) / r.width * 2 - 1, -(e.clientY - r.top) / r.height * 2 + 1);
    ray.setFromCamera(ndc, camera);
    for (const h of ray.intersectObjects(office.groups, true)) {
      for (let o = h.object; o; o = o.parent) if (o.userData.pick) return o.userData.pick;
    }
    return null;
  }
  function floorAt(e) {
    const r = cv.getBoundingClientRect();
    ndc.set((e.clientX - r.left) / r.width * 2 - 1, -(e.clientY - r.top) / r.height * 2 + 1);
    ray.setFromCamera(ndc, camera);
    return ray.ray.intersectPlane(floorPlane, hitPoint) ? hitPoint.clone() : null;
  }
  function disposeGroup(g) {
    g.traverse(o => {if (o.geometry) o.geometry.dispose()});
    scene.remove(g);
  }

  /* ---------------- clicks: a click (not a drag) on an agent selects it; on its monitor or desk it also calls it back;
     in Edit mode on a prop ---------------- */
  const ray = new THREE.Raycaster(), ndc = new THREE.Vector2();
  function pick(e) {
    const r = renderer.domElement.getBoundingClientRect();
    ndc.set((e.clientX - r.left) / r.width * 2 - 1, -(e.clientY - r.top) / r.height * 2 + 1);
    ray.setFromCamera(ndc, camera);
    const h = ray.intersectObjects(pickables, false)[0];
    return h ? h.object.userData.id : null;
  }
  let desks = null, desksL = null;
  const shown = o => {for (; o; o = o.parent) if (!o.visible) return false; return true};
  function deskAt(e) {  // the agent whose desk (its monitor, chair and the rest of it) is the first thing under the pointer
    if (desksL !== L) {desks = desksOf(L, [...S.cit.keys()]); desksL = L}
    if (!desks.size) return null;
    const r = cv.getBoundingClientRect();
    ndc.set((e.clientX - r.left) / r.width * 2 - 1, -(e.clientY - r.top) / r.height * 2 + 1);
    ray.setFromCamera(ndc, camera);
    for (const h of ray.intersectObjects(office.groups, true)) {
      if (!shown(h.object)) continue;
      let uid = null; for (let o = h.object; o && !uid; o = o.parent) uid = o.userData.pick;
      return uid ? deskAgent(desks, uid, h.point.x, h.point.z) : null;
    }
    return null;
  }
  let down = null;
  const cv = renderer.domElement;
  // before OrbitControls sees it (capture on the parent): a press on a prop in Edit mode starts a drag, not a camera turn
  el.addEventListener("pointerdown", e => {
    down = {x: e.clientX, y: e.clientY};
    if (!editing || e.button !== 0 || e.target !== cv) return;
    if (tool) {  // painting the floor, not moving furniture
      const p = floorAt(e); if (!p) return;
      controls.enabled = false; stroke = {tiles: new Map()}; brush(p); cv.setPointerCapture(e.pointerId);
      return;
    }
    const uid = propAt(e), p0 = uid && floorAt(e);
    if (!uid || !p0) return;
    controls.enabled = false;
    drag = {uid, p0, groups: groupsOf(uid).map(q => [q, q.position.clone()]), dx: 0, dz: 0};
    if (uid !== selUid) {selUid = uid; showSel(); editing.onSelect(uid)}
    cv.setPointerCapture(e.pointerId);
  }, {capture: true});
  cv.addEventListener("pointermove", e => {
    if (stroke) {const p = floorAt(e); if (p) brush(p); return}
    if (drag) {
      const p = floorAt(e); if (!p) return;
      const step = e.shiftKey ? 0.5 : 1, dx = Math.round((p.x - drag.p0.x) / step) * step, dz = Math.round((p.z - drag.p0.z) / step) * step;
      if (dx !== drag.dx || dz !== drag.dz) {drag.dx = dx; drag.dz = dz; for (const [q, o] of drag.groups) q.position.set(o.x + dx, o.y, o.z + dz); showSel()}
      return;
    }
    if (!down) cv.style.cursor = editing ? (tool ? "crosshair" : propAt(e) ? "move" : "grab") : pick(e) || deskAt(e) ? "pointer" : "grab";
  });
  cv.addEventListener("pointerup", e => {
    if (stroke) {
      const tiles = [...stroke.tiles.values()]; stroke = null; controls.enabled = true; down = null;
      if (tiles.length && editing.onPaint) editing.onPaint(tool, tiles);
      return;
    }
    if (drag) {
      const d = drag; drag = null; controls.enabled = true; down = null;
      if (d.dx || d.dz) editing.onMove(d.uid, d.dx, d.dz);
      return;
    }
    if (down && Math.hypot(e.clientX - down.x, e.clientY - down.y) < 5) {
      if (editing) {const uid = propAt(e); selUid = uid; showSel(); editing.onSelect(uid)}
      else {const id = pick(e), d = !id && deskAt(e); if (id) ctx.onPick(id); else if (d) (ctx.onMonitor || ctx.onPick)(d)}
    } else if (down) moved = true;
    down = null;
  });
  cv.addEventListener("wheel", () => {moved = true}, {passive: true});
  cv.addEventListener("dblclick", e => {
    if (editing) return;
    const id = pick(e);
    follow = id; moved = true;
    if (id) {A.get(id).closer = true; ctx.onPick(id)}
  });
  el.addEventListener("pointerdown", e => {if (e.button === 2 || e.ctrlKey || e.metaKey || e.shiftKey) follow = null}, {capture: true});  // a pan ends following
  const onKey = e => {if (e.key === "Escape") follow = null};
  document.addEventListener("keydown", onKey);

  /* ---------------- loop ---------------- */
  let last = performance.now();
  let raf = 0, stopped = false;
  // at most 30 frames a second; a slow machine (or software WebGL) drops shadows and draws 15
  let every = 1000 / 30, cost = 0, low = false, tinted = 0;
  function tick() {
    if (stopped) return;
    raf = requestAnimationFrame(tick);
    const t = performance.now();
    if (document.hidden || t - last < every - 2) return;
    const dt = Math.min((t - last) / 1000, 0.1), now = Date.now(); last = t;
    if (!editing) {for (const a of A.values()) {decide(a, now); step(a, dt, now); a.mixer.update(dt)} spread(dt)}
    if (now > tinted) {tintWindows(); tinted = now + 30000}
    track(dt);
    controls.update();
    renderer.render(scene, camera);
    paint(now);
    cost = cost * 0.9 + (performance.now() - t) * 0.1;
    if (!low && cost > 28) {
      low = true; every = 1000 / 15; renderer.setPixelRatio(1); renderer.shadowMap.enabled = false; sun.castShadow = false;
      scene.traverse(o => {if (o.material) o.material.needsUpdate = true}); resize();
    }
  }
  tick();

  return {
    kind: "3d", renderer, scene, camera, controls, agents: A, get idle() {return D},
    get L() {return L},
    stop() {stopped = true; cancelAnimationFrame(raf); ro.disconnect(); controls.dispose(); renderer.dispose(); document.removeEventListener("keydown", onKey); el.innerHTML = ""},
    message(from, to) {const a = A.get(from); if (a && A.has(to)) a.visit = {to, until: Date.now() + 15000}},
    bubble(id, text, secs) {const a = A.get(id); if (!a || !text) return; a.bub.innerHTML = `<span>${esc(text)}</span>`; a.bubUntil = Date.now() + secs * 1000},
    cheer(id, ok) {const a = A.get(id); if (a) a.cheer = {ok, until: Date.now() + 2600}},
    hurry(id) {const a = A.get(id); if (a) a.hurry = Date.now() + 6000},
    recall(id) {const a = A.get(id); if (!a) return; a.visit = null; if (!at(a, a.seat)) a.hurry = Date.now() + 6000},  // office.js keeps it there
    atDesk(id) {const a = A.get(id); return !!a && at(a, a.seat) && !a.path.length},
    deskPos(id) {  // where its monitor (or its desk) is on the page, for tests
      const d = [...desksOf(L, [id])][0]; if (!d) return null;
      const p = L.byUid.get(d[0]), r = cv.getBoundingClientRect(), glow = office.screens.get(S.cit.get(id).home);
      const w = glow ? glow.getWorldPosition(new THREE.Vector3()) : new THREE.Vector3(p.x + p.w / 2, 0.6, p.y + p.h / 2);
      const q = project(w); return {x: r.left + q.x, y: r.top + q.y, monitor: !!glow};
    },
    follow(id) {follow = id && A.has(id) ? id : null; if (follow) {A.get(id).closer = true; moved = true}},
    get following() {return follow},
    windows() {return {daylight: daylight(), color: "#" + glass.color.getHexString()}},
    fit(z) {zoom = z; moved = false; frame(z)},
    resize() {resize(); if (!moved) frame(zoom)},
    screenPos(id) {const a = A.get(id); if (!a) return null; const r = cv.getBoundingClientRect();
      const q = project(new THREE.Vector3(a.x, a.grp.position.y + CHAR_H * 0.55, a.z)); return {x: r.left + q.x, y: r.top + q.y}},
    // Edit mode. handlers: {onSelect(uid | null), onMove(uid, dx, dy) (whole tiles, or halves with Shift),
    // onPaint(tool, [[c, r], ...]) (building tiles)}
    setEdit(on, handlers) {
      editing = on ? handlers : null; selUid = null; drag = null; stroke = null; controls.enabled = true; showSel();
      if (!on) tool = null;
      for (const a of A.values()) a.grp.visible = !on;
      if (gridLines) gridLines.visible = !!on;
      walk.mesh.visible = !!on; showMarkers();
    },
    // what a press on the floor does in Edit mode: null (pick and move furniture), or paint "block" (no-go), "path"
    // (corridor) or "erase"
    setTool(t) {tool = editing && PAINT[t] ? t : null; stroke = null; controls.enabled = true},
    get tool() {return tool},
    get grid() {return G},
    claims,
    // a new layout while editing: the props, floor and walls are rebuilt; the agents are placed again by a new mount
    async setLayout(next) {
      await prepare(ctx, next);
      L = next; disposeGroup(office.group); office = buildOffice(scene, L, S, colors, ctx);
      scene.background.set((L.theme || {}).background || "#12151b");
      walkways(); light(); drawGrid(); drawWalk(); showMarkers();
      if (selUid && !groupsOf(selUid).length) selUid = null;
      showSel();
    },
    select(uid) {selUid = uid || null; showSel()},
    // the tile in the middle of the view (where a new prop goes), and the camera to keep across a new mount
    viewCenter() {return {c: Math.floor(controls.target.x), r: Math.floor(controls.target.z)}},
    view() {return {pos: camera.position.toArray(), target: controls.target.toArray()}},
    get moved() {return moved},  // false while the camera is at a zoom preset (fit, 1x, 2x)
    propScreenPos(uid) {const gs = groupsOf(uid); if (!gs.length) return null; const b = new THREE.Box3(); for (const q of gs) b.expandByObject(q);
      const c = b.getCenter(new THREE.Vector3()), r = cv.getBoundingClientRect(), q = project(c); return {x: r.left + q.x, y: r.top + q.y}},
  };
}
