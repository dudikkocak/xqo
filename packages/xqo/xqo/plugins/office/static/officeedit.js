/* Office view, Edit mode: change this HQ's office in the 3D view and save it to office.json (PUT /api/office/layout).
   Drag props to move them (Shift: half tiles), R turns, Delete removes (an automatic prop is hidden), Ctrl+Z / Ctrl+Y
   undo and redo, Ctrl+D duplicates, arrows nudge, Ctrl+S saves. The side panel adds props from the catalog, sizes rooms,
   picks each agent's character and desk, sets colours, uploads models (.glb) and exports / imports the layout.
   Paths: the floor shows where agents can walk; brush "no-go" or "corridor" paint on it, add or move waypoints (where
   idle agents wander), and see which spots (desk seats first) agents cannot reach.
   Only your own office is edited (not another HQ's, not the planners world); the agents step out while you edit. */
import {layout, zoneAt, entry, turned, LOUNGE, BUILDING} from "/plugins/office/officelayout.js";
import {unreachable} from "/plugins/office/officepath.js";

let X = null;  // from office.js: {S, $, esc, api, build, refresh, select, store, WORLD, VIEW, loadLayout, mergeCatalog}
const clone = o => JSON.parse(JSON.stringify(o));
const r2 = v => Math.round(v * 2) / 2;  // positions snap to half tiles
const uidNew = () => "u-" + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
// only your own office, only in 3D, and only when the server runs with `serve --edit` (it refuses saves otherwise)
const editable = () => X && X.WORLD === "office" && !X.VIEW && !!(X.S.info && X.S.info.editable);

export function init(ctx) {
  X = ctx;
  document.addEventListener("keydown", onKey);
  addEventListener("beforeunload", e => {if (X.S.edit && X.S.edit.dirty) {e.preventDefault(); e.returnValue = ""}});
  X.S.editor = {enter, exit, save, discard, undo, redo, add, move, rotate, remove, duplicate, reset, unhide, roomSize, theme, character, desk, paint, tool,
    importDoc, resetAll, uploadModel, deleteAsset, exportDoc: () => clone(X.S.edit ? X.S.edit.doc : X.S.layout), select: sel};
}

/* ---------------- the header button ---------------- */
export function button() {
  const el = X && X.$("editbtn"); if (!el) return;
  const S = X.S;
  if (!editable() || S.mode !== "3d") {el.innerHTML = ""; return}  // the 2D view is never edited
  el.innerHTML = S.edit ? "" : `<a class="ghost" href="#" id="editgo" title="Move, turn, add and remove furniture, size rooms, pick characters">✎ edit</a>`;
  const a = X.$("editgo");
  if (a) a.onclick = e => {e.preventDefault(); enter()};
}

// after every mount: Edit mode carries over a rebuild while editing. Switching to 2D leaves it (the page reloads),
// and switching back to 3D never starts it.
export function mounted() {
  const S = X.S;
  if (S.edit && S.r.setEdit) {S.r.setEdit(true, handlers); if (S.edit.tool) S.r.setTool(S.edit.tool); draw()}
}

// someone saved the office elsewhere: show it (or, while you edit, say so)
export async function watch() {
  if (!editable()) return;
  let info; try {info = await (await fetch("/api/office")).json()} catch {return}
  if (info.layout_rev === undefined || info.layout_rev === X.S.rev) return;
  if (X.S.edit) {if (!X.S.edit.elsewhere) {X.S.edit.elsewhere = true; draw()} return}
  await X.loadLayout(); await X.build(); X.refresh();
}

/* ---------------- entering and leaving ---------------- */
const handlers = {onSelect: uid => sel(uid), onMove: (uid, dx, dy) => move(uid, dx, dy), onPaint: (t, tiles) => paint(t, tiles)};
async function enter() {
  const S = X.S;
  if (S.edit || !editable() || !S.r.setEdit) return;
  X.select(null);
  S.edit = {doc: clone(S.layout), undo: [], redo: [], sel: null, dirty: false, msg: "", err: "", elsewhere: false};
  delete S.edit.doc.error;
  document.body.classList.add("editing"); document.body.classList.remove("nopanel");
  S.r.setEdit(true, handlers); S.r.resize();
  button(); draw();
}
async function exit(force) {
  const S = X.S, E = S.edit; if (!E) return;
  if (E.dirty && !force && !confirm("Leave Edit mode without saving your changes?")) return;
  S.edit = null;  // the new mount below replaces the editing one
  document.body.classList.remove("editing"); document.body.classList.add("nopanel"); X.$("panel").innerHTML = "";
  X.mergeCatalog(S.layout);
  await X.build(); X.refresh(); button();
}

/* ---------------- changes: each one is a step to undo, and redraws the office ---------------- */
async function relayout() {
  const S = X.S, E = S.edit;
  X.mergeCatalog(E.doc);
  const L = layout(S.zones, E.doc, S.cat, {edit: true});
  await S.r.setLayout(L); S.L = L;
  if (E.sel && !L.byUid.has(E.sel)) E.sel = null;
  S.r.select(E.sel); draw();
}
function change(fn) {
  const E = X.S.edit; if (!E) return;
  const before = JSON.stringify(E.doc);
  fn(E.doc);
  if (JSON.stringify(E.doc) === before) return;
  E.undo.push(before); if (E.undo.length > 200) E.undo.shift();
  E.redo = []; E.dirty = true; E.msg = ""; E.err = "";
  return relayout();
}
function undo() {const E = X.S.edit; if (!E || !E.undo.length) return; E.redo.push(JSON.stringify(E.doc)); E.doc = JSON.parse(E.undo.pop()); E.dirty = true; return relayout()}
function redo() {const E = X.S.edit; if (!E || !E.redo.length) return; E.undo.push(JSON.stringify(E.doc)); E.doc = JSON.parse(E.redo.pop()); E.dirty = true; return relayout()}
function sel(uid) {const E = X.S.edit; if (!E) return; E.sel = uid || null; X.S.r.select(E.sel); draw()}

const prop = uid => X.S.L.byUid.get(uid);
// put a prop at (x, y) (its top-left, in building tiles), turned rot; an added prop belongs to the room it lands in
function place(doc, p, x, y, rot) {
  const L = X.S.L;
  if (p.auto) {
    const o = L.origins[p.zone];
    doc.props = doc.props || {};
    doc.props[p.uid] = {...(doc.props[p.uid] || {}), x: r2(x - o.x), y: r2(y - o.y), rot};
    return;
  }
  const a = (doc.added || []).find(a => a.uid === p.uid); if (!a) return;
  const [w, h] = turned(entry(X.S.cat, p.id), rot), zone = zoneAt(L, X.S.zones, Math.floor(x + w / 2), Math.floor(y + h / 2)), o = L.origins[zone];
  Object.assign(a, {zone, x: r2(x - o.x), y: r2(y - o.y), rot});
}
function move(uid, dx, dy) {const p = prop(uid); if (p) return change(doc => place(doc, p, p.x + dx, p.y + dy, p.rot))}
function rotate(uid, dir = 1) {
  const p = prop(uid); if (!p) return;
  const rot = ((p.rot + 90 * dir) % 360 + 360) % 360, [w, h] = turned(entry(X.S.cat, p.id), rot);
  return change(doc => place(doc, p, Math.round(p.x + p.w / 2 - w / 2), Math.round(p.y + p.h / 2 - h / 2), rot));  // about its centre, on whole tiles
}
function remove(uid) {
  const p = prop(uid); if (!p) return;
  return change(doc => {
    if (p.auto) {doc.props = doc.props || {}; doc.props[uid] = {...(doc.props[uid] || {}), hide: true}}
    else doc.added = (doc.added || []).filter(a => a.uid !== uid);
    for (const a of Object.values(doc.agents || {})) if (a.desk === uid) delete a.desk;
    X.S.edit.sel = null;
  });
}
function addAt(doc, type, x, y, rot) {
  const L = X.S.L, [w, h] = turned(entry(X.S.cat, type), rot), zone = zoneAt(L, X.S.zones, Math.floor(x + w / 2), Math.floor(y + h / 2)), o = L.origins[zone];
  const uid = uidNew();
  (doc.added = doc.added || []).push({uid, type, zone, x: r2(x - o.x), y: r2(y - o.y), rot});
  X.S.edit.sel = uid;
  return uid;
}
function duplicate(uid) {const p = prop(uid); if (p) return change(doc => addAt(doc, p.id, p.x + 1, p.y + 1, p.rot))}
function add(type) {
  if (!X.S.cat.props[type]) return;
  const c = X.S.r.viewCenter(), [w, h] = entry(X.S.cat, type).size;
  const y = type === "large_window" ? 0 : Math.max(2, c.r - Math.floor(h / 2));
  return change(doc => addAt(doc, type, Math.max(0, c.c - Math.floor(w / 2)), y, 0));
}
function reset(uid) {return change(doc => {if (doc.props) delete doc.props[uid]})}
function unhide(uid) {
  return change(doc => {for (const u of uid ? [uid] : X.S.L.hidden) {const o = doc.props && doc.props[u]; if (!o) continue;
    delete o.hide; if (!Object.keys(o).length) delete doc.props[u]}});
}
function roomSize(key, w, h) {
  return change(doc => {doc.rooms = doc.rooms || {};
    if (w == null) delete doc.rooms[key]; else doc.rooms[key] = {size: [Math.max(4, Math.min(200, Math.round(w))), Math.max(4, Math.min(200, Math.round(h)))]}});
}
function theme(k, v) {return change(doc => {doc.theme = doc.theme || {}; if (v) doc.theme[k] = v; else delete doc.theme[k]; if (!Object.keys(doc.theme).length) delete doc.theme})}
function agentSet(doc, agent, k, v) {
  doc.agents = doc.agents || {}; const a = doc.agents[agent] = doc.agents[agent] || {};
  if (v) a[k] = v; else delete a[k];
  if (!Object.keys(a).length) delete doc.agents[agent];
}
function character(agent, id) {return change(doc => agentSet(doc, agent, "character", id))}
// who sits at a desk: an agent moves there (its own desk stays, empty); "" = nobody (back to their own desks)
function desk(uid, agent) {
  return change(doc => {
    for (const [name, a] of Object.entries(doc.agents || {})) if (a.desk === uid) agentSet(doc, name, "desk", "");
    if (agent) {const own = prop(uid); agentSet(doc, agent, "desk", own && own.agent === agent ? "" : uid)}
  });
}
// floor paint ("block" = no-go, "path" = corridor, "erase"): kept per zone, from its top-left like props, so a room
// keeps its paint when the automatic layout moves it
function paint(t, tiles) {
  const L = X.S.L;
  return change(doc => {
    doc.paint = doc.paint || {};
    for (const [c, r] of tiles) {
      const zone = zoneAt(L, X.S.zones, c, r), o = L.origins[zone], x = c - o.x, y = r - o.y, z = doc.paint[zone] = doc.paint[zone] || {};
      for (const k of ["block", "path"]) if (z[k]) z[k] = z[k].filter(([a, b]) => a !== x || b !== y);
      if (t !== "erase") (z[t] = z[t] || []).push([x, y]);
    }
    for (const [zone, z] of Object.entries(doc.paint)) {
      for (const k of ["block", "path"]) if (z[k] && !z[k].length) delete z[k];
      if (!Object.keys(z).length) delete doc.paint[zone];
    }
    if (!Object.keys(doc.paint).length) delete doc.paint;
  });
}
// what a press on the floor does: null (move furniture) or a paint
function tool(t) {const E = X.S.edit; if (!E) return; X.S.r.setTool(t); E.tool = X.S.r.tool; draw()}
async function resetAll() {
  if (!confirm("Go back to the automatic office? (Your uploaded models stay in the catalog.)")) return;
  return change(doc => {for (const k of Object.keys(doc)) if (!["version", "catalog"].includes(k)) delete doc[k]});
}

/* ---------------- saving, files, models ---------------- */
async function call(method, url, body) {
  const r = await fetch(url, {method, headers: {"Content-Type": "application/json"}, body: body === undefined ? undefined : JSON.stringify(body)});
  const d = await r.json().catch(() => null);
  if (!r.ok) {const e = new Error(d && d.detail ? (typeof d.detail === "string" ? d.detail : JSON.stringify(d.detail)) : `${r.status} ${r.statusText}`); e.status = r.status; throw e}
  return d;
}
async function save() {
  const S = X.S, E = S.edit; if (!E) return;
  try {
    const r = await call("PUT", `/api/office/layout?rev=${encodeURIComponent(S.rev)}`, E.doc);
    S.layout = r.layout; S.rev = r.rev; E.doc = clone(r.layout); E.dirty = false; E.elsewhere = false; E.msg = "Saved."; E.err = "";
  } catch (e) {E.err = e.status === 409 ? "Not saved: someone saved this office meanwhile (another tab?). Discard loads their version; export yours first to keep it." : `Not saved: ${e.message}`}
  draw();
}
async function discard() {
  const S = X.S, E = S.edit; if (!E) return;
  if (E.dirty && !confirm("Throw away your unsaved changes?")) return;
  await X.loadLayout();
  Object.assign(E, {doc: clone(S.layout), undo: [], redo: [], dirty: false, elsewhere: false, msg: "", err: "", sel: null});
  delete E.doc.error;
  return relayout();
}
function download() {
  const blob = new Blob([JSON.stringify(X.S.edit.doc, null, 1) + "\n"], {type: "application/json"});
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = `office-${(X.S.info && X.S.info.name) || "hq"}.json`;
  document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}
async function importDoc(doc) {
  const E = X.S.edit; if (!E) return;
  try {
    const r = await call("PUT", "/api/office/layout?check=true", doc);  // checked by the server, nothing saved yet
    await change(d => {for (const k of Object.keys(d)) delete d[k]; Object.assign(d, r.layout)});
    E.msg = "Imported: look it over, then Save.";
  } catch (e) {E.err = `Not imported: ${e.message}`}
  draw();
}
const fileName = n => {
  const [stem, ext] = [n.replace(/\.[^.]*$/, ""), (n.match(/\.([^.]+)$/) || [, "glb"])[1].toLowerCase()];
  return (stem.replace(/[^A-Za-z0-9_.-]+/g, "-").replace(/^[^A-Za-z0-9_-]+/, "").slice(0, 60) || "model") + "." + ext;
};
const asBase64 = f => new Promise((ok, bad) => {const r = new FileReader(); r.onload = () => ok(String(r.result).split(",", 2)[1]); r.onerror = () => bad(r.error); r.readAsDataURL(f)});
// opts: {label, size: [w, d], blocks, kind: "prop" | "character", data (base64, instead of file)}
async function uploadModel(file, opts = {}) {
  const S = X.S, E = S.edit; if (!E) return;
  try {
    const name = fileName(file.name);
    if (!name.endsWith(".glb")) throw new Error("a model is a binary glTF file (.glb): export it from Blender, Blockbench or a Kenney kit as .glb");
    await call("POST", "/api/office/assets", {name, data: opts.data || await asBase64(file)});
    S.assets = await call("GET", "/api/office/assets");
    const id = ("custom-" + name.replace(/\.glb$/, "")).replace(/[^A-Za-z0-9_.-]/g, "-").slice(0, 64), label = (opts.label || name.replace(/\.glb$/, "")).slice(0, 60);
    await change(doc => {
      doc.catalog = doc.catalog || {};
      if (opts.kind === "character") (doc.catalog.characters = doc.catalog.characters || {})[id] = {label, glb: `assets/${name}`};
      else (doc.catalog.props = doc.catalog.props || {})[id] = {label, glb: `assets/${name}`, size: opts.size || [1, 1], blocks: opts.blocks !== false, tags: ["custom"]};
    });
    if (opts.kind !== "character") await add(id);
    E.msg = opts.kind === "character" ? `${label} is a character now: pick it for an agent below, then Save.` : `${label} is in the catalog and placed in the middle of the view: move it, then Save.`;
  } catch (e) {E.err = `Upload failed: ${e.message}`}
  draw();
}
async function deleteAsset(name) {
  const S = X.S, E = S.edit; if (!E) return;
  const used = Object.entries({...((E.doc.catalog || {}).props || {}), ...((E.doc.catalog || {}).characters || {})}).filter(([, c]) => c.glb === `assets/${name}`).map(([k]) => k);
  if (!confirm(`Delete ${name} from this HQ?${used.length ? " Props using it are removed from the catalog; placed ones show as pink boxes until you remove them." : ""}`)) return;
  try {
    await call("DELETE", `/api/office/assets/${encodeURIComponent(name)}`);
    S.assets = await call("GET", "/api/office/assets");
    if (used.length) await change(doc => {for (const k of used) {if (doc.catalog.props) delete doc.catalog.props[k]; if (doc.catalog.characters) delete doc.catalog.characters[k]}});
  } catch (e) {E.err = e.message}
  draw();
}

/* ---------------- keys ---------------- */
function onKey(e) {
  const E = X.S.edit; if (!E) return;
  if (/INPUT|TEXTAREA|SELECT/.test((e.target && e.target.tagName) || "")) return;
  const k = e.key, mod = e.ctrlKey || e.metaKey, s = E.sel, step = e.shiftKey ? 0.5 : 1;
  const act = {
    z: mod && !e.shiftKey ? undo : mod && e.shiftKey ? redo : null, Z: mod ? redo : null, y: mod ? redo : null,
    s: mod ? save : null, d: mod && s ? () => duplicate(s) : null,
    r: !mod && s ? () => rotate(s, 1) : null, R: !mod && s ? () => rotate(s, -1) : null,
    Delete: s ? () => remove(s) : null, Backspace: s ? () => remove(s) : null, Escape: () => E.tool ? tool(null) : sel(null),
    ArrowLeft: s ? () => move(s, -step, 0) : null, ArrowRight: s ? () => move(s, step, 0) : null,
    ArrowUp: s ? () => move(s, 0, -step) : null, ArrowDown: s ? () => move(s, 0, step) : null,
  }[k];
  if (act) {e.preventDefault(); act()}
}

/* ---------------- the side panel ---------------- */
const zoneName = key => key === LOUNGE ? "lounge" : key === BUILDING ? "building" : (X.S.zones.find(z => z.key === key) || {}).label || `${key} (gone)`;
function draw() {
  const S = X.S, E = S.edit, el = X.$("panel"), esc = X.esc; if (!E) return;
  const L = S.L, p = E.sel && prop(E.sel), e = p && entry(S.cat, p.id);
  const agents = [...S.cit.values()].map(c => c.agent);
  const occupant = p && p.anchors.some(a => a.type === "work") ? [...L.homes].find(([, a]) => p.anchors.some(x => x.name === a)) : null;
  const groups = {};
  for (const [id, c] of Object.entries(S.cat.props)) (groups[(c.tags || ["other"])[0]] = groups[(c.tags || ["other"])[0]] || []).push([id, c]);
  const th = E.doc.theme || {}, scroll = el.scrollTop;
  const lost = unreachable(L), label = u => {const q = prop(u); return q ? entry(S.cat, q.id).label || q.id : u};
  const TOOLS = [[null, "move furniture"], ["block", "no-go"], ["path", "corridor"], ["erase", "erase paint"]];
  el.innerHTML = `
    <h3>Edit the office <span class="spacer" style="flex:1"></span><button class="ghost" data-a="done" title="Back to the live office">Done</button></h3>
    <div class="row"><button data-a="save" ${E.dirty ? "" : "disabled"} title="Ctrl+S">Save</button><button class="ghost" data-a="discard" ${E.dirty || E.elsewhere ? "" : "disabled"}>Discard</button>
      <button class="ghost" data-a="undo" ${E.undo.length ? "" : "disabled"} title="Ctrl+Z">↶ undo</button><button class="ghost" data-a="redo" ${E.redo.length ? "" : "disabled"} title="Ctrl+Y">↷ redo</button>
      <span class="sub">${E.dirty ? "unsaved changes" : "saved"}</span></div>
    ${E.elsewhere ? '<div class="err">Someone saved this office meanwhile (another tab?). Saving now fails; Discard loads theirs.</div>' : ""}
    ${E.err ? `<div class="err">${esc(E.err)}</div>` : ""}${E.msg ? `<div class="hint ok">${esc(E.msg)}</div>` : ""}
    ${S.layoutError ? `<div class="err">${esc(S.layoutError)}</div>` : ""}
    <div class="hint">Drag furniture to move it (Shift: half tiles) · R turns · Del removes · arrows nudge · drag the floor to look around.</div>
    <h2>Selected</h2>
    ${p ? `<div class="box"><div><b>${esc(e.label || p.id)}</b> <span class="sub">${esc(p.id)}${e.missing ? " · not in the catalog" : ""}</span></div>
      <div class="sub">${esc(zoneName(p.zone))} · tile ${p.x}, ${p.y} · ${p.w}×${p.h} · turned ${p.rot}°${p.auto ? " · automatic" + (p.changed ? ", changed" : "") : " · added"}</div>
      <div class="row"><button class="ghost" data-a="rotl" title="Shift+R">⟲ turn</button><button class="ghost" data-a="rotr" title="R">⟳ turn</button>
        <button class="ghost" data-a="dup" title="Ctrl+D">duplicate</button><button class="ghost" data-a="del" title="Delete">${p.auto ? "hide" : "remove"}</button>
        ${p.auto && p.changed ? '<button class="ghost" data-a="reset">back to automatic</button>' : ""}</div>
      ${occupant !== null ? `<div class="row"><span class="sub">Who sits here</span><select data-desk>${["", ...agents].map(a => `<option value="${esc(a)}"${(occupant ? occupant[0] : "") === a ? " selected" : ""}>${a ? esc(a) : "(nobody)"}</option>`).join("")}</select></div>` : ""}
      </div>` : '<div class="hint">Click a piece of furniture to select it.</div>'}
    <h2>Paths</h2>
    <div class="hint" style="margin-top:0">On the floor: red is blocked, dark red no-go paint, green a corridor (agents prefer it); coloured squares are the spots agents use, magenta ones they cannot reach. Idle agents wander between waypoints (Add › paths).</div>
    <div class="row">${TOOLS.map(([t, n]) => `<button class="ghost${(E.tool || null) === t ? " on" : ""}" data-tool="${t || ""}" title="${t ? "drag over the floor to paint" : "drag furniture"}">${n}</button>`).join("")}
      ${E.doc.paint ? '<button class="ghost" data-a="paintclear" title="remove all floor paint">clear paint</button>' : ""}</div>
    ${lost.map(u => u.agent ? `<div class="err">⚠ ${esc(u.agent)}'s desk cannot be reached <button class="ghost" data-pick="${esc(u.uid)}">show</button></div>` : "").join("")}
    ${lost.some(u => !u.agent) ? `<div class="hint">Spots agents cannot reach: ${lost.filter(u => !u.agent).map(u => `<button class="ghost" data-pick="${esc(u.uid)}">${esc(label(u.uid))}</button>`).join(" ")}</div>` : ""}
    <h2>Add</h2>
    ${Object.entries(groups).map(([g, list]) => `<div class="sub">${esc(g)}</div><div class="palette">${list.map(([id, c]) => `<button class="ghost" data-add="${esc(id)}" title="${esc(id)} · ${c.size[0]}×${c.size[1]} tiles">${esc(c.label || id)}</button>`).join("")}</div>`).join("")}
    <h2>Rooms</h2>
    ${S.zones.map(z => `<div class="row room"><span class="n">${esc(z.label)}</span><input type="number" min="4" max="200" value="${z.w}" data-room="${esc(z.key)}" data-dim="w" title="width in tiles">×<input type="number" min="4" max="200" value="${z.h}" data-room="${esc(z.key)}" data-dim="h" title="depth in tiles">
      ${E.doc.rooms && E.doc.rooms[z.key] ? `<button class="ghost" data-roomauto="${esc(z.key)}" title="automatic: ${z.autoSize.join("×")}">auto</button>` : `<span class="sub">auto</span>`}</div>`).join("")}
    <h2>Agents</h2>
    ${agents.map(a => {const cur = ((E.doc.agents || {})[a] || {}).character || "";
      return `<div class="row room"><span class="n">${esc(a)}</span><select data-char="${esc(a)}"><option value="">(automatic)</option>${Object.entries(S.cat.characters).map(([id, c]) => `<option value="${esc(id)}"${cur === id ? " selected" : ""}>${esc(c.label || id)}</option>`).join("")}</select></div>`}).join("")}
    <h2>Colours</h2>
    <div class="row">${[["floor", "#b9bec6", "hall floor"], ["wall", "#c96f53", "back wall"], ["background", "#12151b", "background"]].map(([k, d, t]) =>
      `<label class="sub">${t} <input type="color" value="${esc(th[k] || d)}" data-theme="${k}"></label>`).join("")}${E.doc.theme ? '<button class="ghost" data-a="themeauto">reset</button>' : ""}</div>
    ${L.hidden.length ? `<h2>Hidden (${L.hidden.length})</h2><div class="palette">${L.hidden.map(u => `<button class="ghost" data-unhide="${esc(u)}" title="show it again">${esc(u)}</button>`).join("")}</div>` : ""}
    ${L.orphans.length ? `<div class="hint">${L.orphans.length} added prop${L.orphans.length > 1 ? "s" : ""} of departments that are gone are kept in the file, not shown.</div>` : ""}
    <h2>Your models</h2>
    <div class="box"><div class="hint" style="margin-top:0">A .glb model (Kenney kits, Blockbench, Blender…). It is kept on this HQ, in office-assets/ next to the artifacts.</div>
      <div class="row"><input type="file" id="mfile" accept=".glb,model/gltf-binary"></div>
      <div class="row"><input id="mlabel" placeholder="name" style="flex:1;min-width:0"><select id="mkind"><option value="prop">furniture</option><option value="character">character</option></select></div>
      <div class="row" id="msize"><span class="sub">size</span><input type="number" id="mw" value="1" min="0.25" max="40" step="0.5" title="width in tiles">×<input type="number" id="md" value="1" min="0.25" max="40" step="0.5" title="depth in tiles">
        <label class="sub"><input type="checkbox" id="mblocks" checked> agents walk around it</label></div>
      <div class="row"><button id="mup">Upload</button></div>
      ${S.assets.length ? S.assets.map(a => `<div class="row room"><span class="n" title="${esc(a.name)}">${esc(a.name)}</span><span class="sub">${Math.ceil(a.size / 1024)} KB</span><button class="ghost" data-delasset="${esc(a.name)}">delete</button></div>`).join("") : ""}</div>
    <h2>Layout file</h2>
    <div class="row"><button class="ghost" data-a="export" title="office.json, to keep or to load into another HQ">export</button><button class="ghost" data-a="import">import…</button><input type="file" id="ifile" accept=".json,application/json" style="display:none">
      <button class="ghost" data-a="auto" title="Undo everything back to the automatic layout (Save to keep it)">automatic office</button></div>`;
  const on = (sel, ev, fn) => el.querySelectorAll(sel).forEach(n => n[ev] = () => fn(n));
  const A = {done: () => exit(), save, discard, undo, redo, rotl: () => rotate(E.sel, -1), rotr: () => rotate(E.sel, 1), dup: () => duplicate(E.sel),
    del: () => remove(E.sel), reset: () => reset(E.sel), themeauto: () => change(d => delete d.theme), export: download, paintclear: () => change(d => delete d.paint),
    import: () => X.$("ifile").click(), auto: resetAll};
  on("[data-a]", "onclick", n => A[n.dataset.a]());
  on("[data-add]", "onclick", n => add(n.dataset.add));
  on("[data-tool]", "onclick", n => tool(n.dataset.tool || null));
  on("[data-pick]", "onclick", n => sel(n.dataset.pick));
  on("[data-unhide]", "onclick", n => unhide(n.dataset.unhide));
  on("[data-roomauto]", "onclick", n => roomSize(n.dataset.roomauto, null));
  on("[data-room]", "onchange", n => {const z = S.zones.find(z => z.key === n.dataset.room), v = Number(n.value);
    if (z && v) roomSize(z.key, n.dataset.dim === "w" ? v : z.w, n.dataset.dim === "h" ? v : z.h)});
  on("[data-char]", "onchange", n => character(n.dataset.char, n.value));
  on("[data-theme]", "onchange", n => theme(n.dataset.theme, n.value));
  on("[data-desk]", "onchange", n => desk(E.sel, n.value));
  on("[data-delasset]", "onclick", n => deleteAsset(n.dataset.delasset));
  X.$("mkind").onchange = () => {X.$("msize").style.display = X.$("mkind").value === "prop" ? "" : "none"};
  X.$("mup").onclick = () => {const f = X.$("mfile").files[0]; if (!f) {E.err = "Choose a .glb file first."; return draw()}
    uploadModel(f, {label: X.$("mlabel").value.trim(), kind: X.$("mkind").value, size: [Number(X.$("mw").value) || 1, Number(X.$("md").value) || 1], blocks: X.$("mblocks").checked})};
  X.$("ifile").onchange = async () => {const f = X.$("ifile").files[0]; if (!f) return;
    try {await importDoc(JSON.parse(await f.text()))} catch (err) {E.err = `Not imported: ${err.message}`; draw()}};
  el.scrollTop = scroll;
}
