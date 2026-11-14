/* Office view: the HQ as an office (3D low-poly with three.js, or the flat miniverse pixel office), or the planners
   of every HQ in one room. It only draws. Every action in the side panel calls the dashboard API (another HQ's
   through the office relay). The drawing lives in office3d.js / office2d.js, the layout in officelayout.js and the
   Edit mode in officeedit.js; the state and panel live here. */
import {layout} from "/plugins/office/officelayout.js";
import * as editor from "/plugins/office/officeedit.js";
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const debounce = (fn, ms) => {let t; return (...a) => {clearTimeout(t); t = setTimeout(() => fn(...a), ms)}};
const store = {get(k, d) {try {const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v)} catch {return d}},
               set(k, v) {try {localStorage.setItem(k, JSON.stringify(v))} catch {}}};
const params = new URLSearchParams(location.search);
const WORLD = params.get("world") === "planners" ? "planners" : "office";
const VIEW = params.get("hq") || "";  // another HQ's office, read through the relay
const MODE = ["2d", "3d"].includes(params.get("view")) ? params.get("view") : store.get("office.view", "3d");  // renderer
const COLORS = ["#7aa2ff", "#4fd1c5", "#f6ad55", "#ed64a6", "#9f7aea", "#68d391", "#ecc94b", "#90cdf4", "#fc8181"];
const MSG = new Set(["direct", "handover", "result", "reply", "offer", "user_message", "agent_reply", "peer"]);
const ACTIVE = new Set(["pending", "running", "needs_attention", "paused"]);

/* ---------------- sources: this HQ, or another one through the relay ---------------- */
function source(name, remote, url) {
  const base = remote ? `/api/office/hq/${encodeURIComponent(name)}/` : "/api/";
  return {name, remote, url: url || "", alias: name, planners: [], up: false,
    path: p => base + p, events: remote ? `${base}events` : "/api/events",
    dash: root => `${remote ? url : ""}/?task=${encodeURIComponent(root)}`};
}
async function api(src, p, opt) {
  const r = await fetch(src.path(p), opt && {method: "POST", headers: {"Content-Type": "application/json"}, ...opt});
  if (!r.ok) {const d = await r.json().catch(() => null); throw new Error(d && d.detail ? (typeof d.detail === "string" ? d.detail : JSON.stringify(d.detail)) : `${r.status} ${r.statusText}`)}
  return r.json();
}

const S = {info: null, sources: [], zones: [], cit: new Map(), r: null, mode: MODE, sel: null, task: "", involved: null,
  tasks: new Map(), ev: 0, panelTask: null, chatKind: "ask", title: "", bubbles: new Map(), unseen: store.get("office.unseen", {}),
  cat: {props: {}, characters: {}}, shipped: null, layout: {version: 1}, rev: "", assets: [], edit: null};

/* ---------------- the catalog and the edited layout (office.json) of the office shown ---------------- */
async function loadLayout() {
  if (!S.shipped) S.shipped = await (await fetch("/plugins/office/office3d/catalog.json")).json();
  S.layout = {version: 1}; S.rev = ""; S.assets = []; S.layoutError = "";
  if (WORLD === "office") {
    try {const r = await api(S.sources[0], "office/layout"); S.layout = r.layout || {version: 1}; S.rev = r.rev || ""; S.assets = r.assets || []}
    catch (e) {S.layoutError = e.message}  // an HQ from before office editing: automatic layout
    if (S.layout.error) S.layoutError = S.layout.error;
  }
  mergeCatalog(S.layout);
}
function mergeCatalog(doc) {
  const own = (doc && doc.catalog) || {};
  S.cat = {props: {...S.shipped.props, ...(own.props || {})}, characters: {...S.shipped.characters, ...(own.characters || {})}};
}

/* ---------------- who is in the world ---------------- */
async function loadZones() {
  const zones = [];
  if (WORLD === "office") {
    const src = S.sources[0];
    let depts;
    try {depts = await api(src, "departments")}
    catch {depts = [{name: "", agents: await api(src, "agents")}]}  // no departments: one team room
    for (const d of depts) {
      const members = (d.agents || []).map(a => ({id: a.name, src, agent: a.name, nick: a.nickname || "", av: a.avatar || null, name: (a.nickname || a.name) + (a.head ? " ★" : ""), dept: d.name}));
      if (members.length) zones.push({key: d.name || "team", label: d.name === "hq" ? S.hqName || "Headquarters" : d.name || S.hqName || "Team", members, src});
    }
  } else {
    await Promise.all(S.sources.map(async src => {
      try {const o = await api(src, "office"); src.planners = o.planners || []; src.nicks = o.nicknames || {}; src.avs = o.avatars || {}; src.alias = o.name || src.name; src.title = o.title || ""; src.up = true}
      catch {src.planners = []; src.nicks = {}; src.avs = {}; src.up = false}
    }));
    for (const src of S.sources) {
      const members = (src.planners.length ? src.planners : ["planner"]).map(p => ({id: `${src.name}/${p}`, src, agent: p, nick: (src.nicks || {})[p] || "", av: avatarFor(src, p), name: `${src.name}/${(src.nicks || {})[p] || p}`, dept: src.name}));
      zones.push({key: src.name, label: (src.title || src.name) + (src.remote ? "" : " (this HQ)"), members, src, hq: src.name, down: !src.up});
    }
  }
  return zones;
}

const lookOf = s => [...s].reduce((h, c) => (h * 31 + c.charCodeAt(0)) >>> 0, 7);  // which character model / sprite

let building = null;
async function build() {
  if (building) return building;
  building = (async () => {
    const zones = await loadZones(), L = layout(zones, S.edit ? S.edit.doc : S.layout, S.cat, {edit: WORLD === "office"});
    // a rebuild keeps the camera where the user put it; a fresh start of the 3D office restores the last one saved
    const view = S.r ? (S.r.view && S.r.moved ? S.r.view() : null) : S.mode === "3d" ? savedCam() : null;
    if (S.r) {S.r.stop(); S.r = null}
    const old = S.cit; S.cit = new Map();
    for (const z of zones) for (const m of z.members) {
      const prev = old.get(m.id) || {};
      S.cit.set(m.id, {...m, zone: z, home: L.homes.get(m.id), look: lookOf(m.id), busy: prev.busy || 0, paused: prev.paused || 0,
        att: prev.att || [], flash: null, title: prev.title || "", recall: prev.recall || 0});
    }
    S.zones = zones;
    const ctx = {el: $("world"), L, S, colors: COLORS, statuses, stateOf, alertOf, recalled, idle: S.info && S.info.idle, zoneLinks: WORLD === "planners", view, doneOf,
      zoneColor: c => COLORS[Math.max(0, zones.indexOf(c.zone)) % COLORS.length],
      cat: S.cat, characters: Object.keys(S.shipped.characters), asset: name => S.sources[0].path(`office/assets/${encodeURIComponent(name)}`),
      onPick: id => select(id), onMonitor: id => {select(id); recall(id)}, onZone: z => {location.href = z.src.remote ? `/office?hq=${encodeURIComponent(z.hq)}` : "/office"}};
    S.r = await mountRenderer(ctx);
    S.L = L;
    window.office = S; /* for the browser console */
    editor.mounted();
    if (!view) fit();
    $("empty").style.display = S.cit.size ? "none" : "";
    $("empty").textContent = S.cit.size ? "" : "No agents here yet.";
  })();
  try {await building} finally {building = null}
}

/* ---------------- the 3D camera, saved per office (this HQ, another HQ, the planners room) for the next start ---------------- */
const CAM_KEY = `office.camera3d.${WORLD}.${VIEW || "local"}`;
const vec3 = v => Array.isArray(v) && v.length === 3 && v.every(Number.isFinite);
function savedCam() {const v = store.get(CAM_KEY, null); return v && vec3(v.pos) && vec3(v.target) ? {pos: v.pos, target: v.target} : null}
let camLast = "";
function saveCam() {
  if (!S.r || S.r.kind !== "3d" || !S.r.view) return;
  const v = S.r.view(), cam = {pos: v.pos.map(n => +n.toFixed(3)), target: v.target.map(n => +n.toFixed(3))}, txt = JSON.stringify(cam);
  if (txt !== camLast) {camLast = txt; store.set(CAM_KEY, cam)}  // always the last view, a zoom preset too
}

/* ---------------- renderer: 3D (three.js) by default, the 2D pixel office as the fallback ---------------- */
async function mountRenderer(ctx) {
  document.body.classList.toggle("v3d", S.mode === "3d");
  if (S.mode === "3d") {
    try {return await (await import("/plugins/office/office3d.js")).mount(ctx)}
    catch (e) {console.error("3D office unavailable, using the 2D office:", e); S.mode = "2d"; S.fallback = String(e.message || e);
      document.body.classList.remove("v3d"); ctx.el.innerHTML = ""; viewButtons()}
  }
  return (await import("/plugins/office/office2d.js")).mount(ctx);
}
function viewButtons() {
  editor.button();
  const b = m => `<a class="ghost${S.mode === m ? " on" : ""}" href="#" data-view="${m}" title="${m === "3d" ? "Low-poly 3D office (drag to rotate, wheel to zoom, right-drag to pan)" : "Flat pixel office"}${S.fallback && m === "3d" ? ` · not available here: ${esc(S.fallback)}` : ""}">${m.toUpperCase()}</a>`;
  const views = (S.info && S.info.views && S.info.views.length) ? S.info.views : ["3d", "2d"];
  $("views").innerHTML = views.length > 1 ? views.map(b).join(" ") : "";  // one view configured: no switch
  $("views").querySelectorAll("[data-view]").forEach(n => n.onclick = e => {e.preventDefault(); store.set("office.view", n.dataset.view);
    const p = new URLSearchParams(location.search); p.delete("view"); location.href = location.pathname + (String(p) ? "?" + p : "")});
}

/* ---------------- state: what each citizen shows ---------------- */
function stateOf(c) {
  if (S.involved && !S.involved.has(c.id)) return "offline";  // task filter: only the agents on that task
  if (c.att.length) return "error";
  if (c.flash && c.flash.until > Date.now()) return c.flash.state;
  if (c.busy > 0) return "working";
  if (c.paused > 0) return "sleeping";
  return "idle";
}
function statuses() {
  return [...S.cit.values()].map(c => ({id: c.id, name: c.name, state: stateOf(c), energy: 1,
    task: c.att.length ? `needs you: ${c.att[0].question.slice(0, 60)}` : c.title || null}));
}
const short = (s, n = 70) => {s = String(s || "").replace(/\s+/g, " ").trim(); return s.length > n ? s.slice(0, n - 1) + "…" : s};
const titleOf = name => short(String(name || "").split(" : ").slice(1).join(" : ") || name, 50);

function idOf(src, agent) {
  if (!agent) return null;
  if (WORLD === "office") return S.cit.has(agent) ? agent : null;
  const m = /^peer:(.+)$/.exec(agent);
  if (m) {const s = S.sources.find(x => x.alias === m[1] || x.name === m[1]); return s ? [...S.cit.values()].find(c => c.src === s)?.id || null : null}
  const id = `${src.name}/${agent}`;
  return S.cit.has(id) ? id : null;
}
/* speech bubbles: up to 4 lines, at least 5 seconds each; a burst waits its turn (a short queue per agent) */
const BUBBLE_MIN = 5;
function bubble(id, text, secs) {
  if (!S.r || !text || !id) return;
  text = short(text, 160);
  const b = S.bubbles.get(id) || {until: 0, queue: []}; S.bubbles.set(id, b);
  if (Date.now() >= b.until) return showBubble(id, b, text, secs);
  b.queue.push([text, secs]); if (b.queue.length > 2) b.queue.shift();  // keep the newest
}
function bubbleNow(id, text, secs) {  // the user's own click: shown at once, ahead of any queued bubble
  if (!S.r || !text || !id) return;
  const b = S.bubbles.get(id) || {until: 0, queue: []}; S.bubbles.set(id, b); showBubble(id, b, text, secs);
}
function showBubble(id, b, text, secs) {
  const s = Math.max(BUBBLE_MIN, secs || Math.min(10, 3 + text.length / 25));
  S.r.bubble(id, text, s); b.until = Date.now() + BUBBLE_MIN * 1000;
}
function nextBubbles() {
  const now = Date.now();
  for (const [id, b] of S.bubbles) if (b.queue.length && now >= b.until && S.r) showBubble(id, b, ...b.queue.shift());
}

/* a finished task: its agent cheers (or shakes its head) and keeps a ✓ until you look at it */
/* what the agent's monitor flashes until it is dealt with: an open question, an error, an auto-answered question,
   a finished task (alerts.py); null when nothing needs the user */
const ALERT_RANK = {attention: 4, error: 3, auto_confirmed: 2, done: 1};
function alertOf(c) {
  if (c.att.length) return "attention";
  let best = null;
  for (const d of doneOf(c)) {const k = d.kind || (d.ok ? "done" : "error"); if (!best || ALERT_RANK[k] > ALERT_RANK[best]) best = k}
  return best;
}
function doneOf(c) {  // finished / failed / auto-answered: the server's alerts (alerts.py), or this browser's list for an older HQ
  if (c.alerts) return c.alerts.map(a => ({task: a.task_id, ok: a.kind === "done", name: a.text, id: a.id, kind: a.kind}));
  return S.unseen[c.id] || [];
}
function finished(id, e) {
  const st = (e.meta || {}).status || "done", ok = st === "done", c = S.cit.get(id);
  const list = (S.unseen[id] || []).filter(x => x.task !== e.task_id).concat({task: e.task_id, ok, name: e.task_name || ""}).slice(-9);
  S.unseen[id] = list; store.set("office.unseen", S.unseen);
  if (!ok) c.flash = {state: "error", until: Date.now() + 6000};
  if (S.r && S.r.cheer) S.r.cheer(id, ok);
  bubble(id, `${ok ? "✓ Done" : st === "cancelled" ? "Cancelled" : "✗ Failed"}: ${titleOf(e.task_name) || "task"}`, 6);
}
function seen(id) {
  if (id && S.unseen[id]) {delete S.unseen[id]; store.set("office.unseen", S.unseen)}
}
function onEvent(src, e) {
  staleReps(src, e);
  if (e.kind === "departments" && WORLD === "office") return rebuildSoon();
  const sid = idOf(src, e.sender), rid = idOf(src, e.recipient);
  if (sid && e.task_name && !["done", "error", "result", "reply"].includes(e.kind)) S.cit.get(sid).title = titleOf(e.task_name);
  if (rid && e.kind === "task" && e.task_name) S.cit.get(rid).title = titleOf(e.task_name);
  if (rid && e.kind === "task" && e.sender === "user") hurry(rid);  // a new task: to the desk at once
  if (sid && e.kind === "ack") {hurry(sid); bubble(sid, e.content)}  // the server's instant "got it"
  if (sid && e.kind === "done" && !e.parent_task_id && e.recipient === "user") finished(sid, e);
  if (MSG.has(e.kind) && sid) {
    // a peer message between two HQs in this room: animate the sending side only
    const incoming = e.kind === "peer" && /^peer:/.test(e.sender || "");
    if (incoming && S.sources.some(s => s !== src && (s.alias === e.sender.slice(5) || s.name === e.sender.slice(5)))) return;
    const c = S.cit.get(sid);
    if (rid && rid !== sid && stateOf(c) !== "offline" && stateOf(S.cit.get(rid)) !== "offline") {
      S.r.message(sid, rid);
      c.flash = {state: "speaking", until: Date.now() + 7000};
    }
    bubble(sid, e.content);
  } else if (e.kind === "error" && sid) {
    S.cit.get(sid).flash = {state: "error", until: Date.now() + 6000};
  }
  if (S.task && (e.root_task_id === S.taskRoot) && (src.name === S.taskSrc)) involvedSoon();
  refreshSoon();
}

function hurry(id) {
  const c = S.cit.get(id); if (!c || stateOf(c) === "offline") return;
  c.flash = {state: "working", until: Date.now() + 8000};  // until the next poll says it works
  if (S.r && S.r.hurry) S.r.hurry(id);
}

/* ---------------- polling: busy / paused / waiting requests ---------------- */
async function refresh() {
  await Promise.all(S.sources.map(async src => {
    try {
      const [agents, att, usage, alerts] = await Promise.all([api(src, "agents"), api(src, "attention?pending=true"), api(src, "usage").catch(() => null),
        api(src, "alerts").catch(() => null)]);
      src.usage = usage;
      for (const a of agents) {
        const c = S.cit.get(idOf(src, a.name)); if (!c) continue;
        c.busy = a.active || 0; c.paused = a.paused || 0;
        c.att = att.filter(x => x.agent === a.name && x.status === "pending");
        c.alerts = alerts ? alerts.items.filter(x => x.agent === a.name && x.kind !== "attention") : null;
      }
      src.up = true;
    } catch {src.up = false}
  }));
  status(); usageLine();
  if (S.sel) drawPanel(false);
}
function usageLine() {  // the overall total and plan limit in the middle of the header (every HQ of the room in the planners world)
  usageHeader.render(S.sources.map(s => s.usage), $("usage"), $("ratelim"), WORLD === "planners" ? "of all HQs here" : "of this HQ");
}
const refreshSoon = debounce(refresh, 300);
const rebuildSoon = debounce(() => S.edit ? (S.edit.stale = true) : build().then(refresh), 500);  // not under someone editing
function status() {
  const down = S.sources.filter(s => !s.up).map(s => s.name);
  $("dot").classList.toggle("on", !down.length);
  const n = S.cit.size, busy = [...S.cit.values()].filter(c => c.busy > 0).length, att = [...S.cit.values()].reduce((k, c) => k + c.att.length, 0);
  $("status").textContent = `${n} agent${n === 1 ? "" : "s"} · ${busy} working` + (att ? ` · ${att} waiting for you` : "") + (down.length ? ` · not reachable: ${down.join(", ")}` : "");
}

/* ---------------- task filter ---------------- */
async function loadTasks() {
  const opts = ['<option value="">all agents</option>'];
  for (const src of S.sources) {
    let list = [];
    try {list = await api(src, "tasks")} catch {continue}
    for (const t of list) S.tasks.set(`${src.name}|${t.id}`, {...t, src});
    const roots = list.filter(t => !t.parent_id).sort((a, b) => (ACTIVE.has(b.status) - ACTIVE.has(a.status)) || String(b.created).localeCompare(String(a.created))).slice(0, 60);
    for (const t of roots) opts.push(`<option value="${esc(src.name + "|" + t.id)}">${esc((S.sources.length > 1 ? src.name + "/" : "") + t.status.replace("_", " ") + " · " + t.name)}</option>`);
  }
  const cur = $("taskf").value; $("taskf").innerHTML = opts.join(""); $("taskf").value = cur;
  if ($("taskf").value !== cur) setTask("");
}
async function involved() {
  if (!S.task) {S.involved = null; return}
  const t = S.tasks.get(S.task); if (!t) return;
  const ids = new Set();
  try {
    (function walk(n) {for (const a of [n.assignee, n.requester]) {const id = idOf(t.src, a); if (id) ids.add(id)} (n.children || []).forEach(walk)})(await api(t.src, `tasks/${encodeURIComponent(t.id)}`));
  } catch {}
  S.involved = ids;
}
const involvedSoon = debounce(involved, 500);
async function setTask(v) {
  S.task = v; const t = S.tasks.get(v); S.taskRoot = t ? t.id : null; S.taskSrc = t ? t.src.name : null;
  store.set("office.task", v); await involved(); if (S.sel) drawPanel(false);
}

/* ---------------- clicks (the renderer calls select / onMonitor / onZone) ---------------- */
function select(id) {
  if (S.edit) return;
  if (S.sel && S.sel !== id && S.cit.get(S.sel)) S.cit.get(S.sel).recall = 0;  // closing (or switching) the panel lets it go again
  S.sel = id; S.panelTask = null; document.body.classList.toggle("nopanel", !id); seen(id);
  if (id) drawPanel(true); else $("panel").innerHTML = "";
  if (S.r) S.r.resize();
}

/* a click on an agent's monitor (or its desk): it comes back to its seat and stays there while its panel is open, at
   most S.recallMs. Only the office's own picture: nothing is sent to the agent. Working or waiting agents are there already. */
S.recallMs = 60000;
function recall(id) {
  const c = S.cit.get(id); if (!c || S.edit) return;
  const st = stateOf(c);
  if (st !== "idle" && st !== "sleeping") return;
  const away = !(S.r && S.r.atDesk && S.r.atDesk(id));
  c.recall = Date.now() + S.recallMs;
  if (S.r && S.r.recall) S.r.recall(id);
  if (away) bubbleNow(id, "On my way", 3);
}
const recalled = c => !!c && c.recall > Date.now();

/* ---------------- side panel: everything here goes through the dashboard API ---------------- */
async function drawPanel(fresh) {
  if (S.edit) return;
  const c = S.cit.get(S.sel), el = $("panel");
  if (!c) {select(null); return}
  if (!fresh && el.contains(document.activeElement) && /TEXTAREA|INPUT/.test(document.activeElement.tagName)) {paintHeader(c); return}  // don't wipe what the user is typing
  let tasks = [], all = [];
  try {all = await api(c.src, "tasks"); tasks = all.filter(t => t.assignee === c.agent)} catch {}
  if (!fresh && el.contains(document.activeElement) && /TEXTAREA|INPUT/.test(document.activeElement.tagName)) {paintHeader(c); return}  // the user started typing meanwhile
  tasks.sort((a, b) => (ACTIVE.has(b.status) - ACTIVE.has(a.status)) || String(b.created).localeCompare(String(a.created)));
  const filterRoot = S.taskSrc === c.src.name ? S.taskRoot : null;
  if (!S.panelTask) {const t = tasks.find(t => filterRoot && t.root_id === filterRoot) || tasks.find(t => ACTIVE.has(t.status)); S.panelTask = t ? t.id : null}
  const pt = tasks.find(t => t.id === S.panelTask);
  // pause / resume act on the whole task tree, so they follow its root: resume a paused one, or reopen a finished one
  const root = pt && (all.find(t => t.id === pt.root_id) || pt), rst = root && root.status;
  const ctl = !pt ? "" : rst === "paused" ? '<button class="ghost" data-act="resume" title="Continue the paused task">▶ resume</button>'
    : ACTIVE.has(rst) ? '<button class="ghost" data-act="pause">⏸ pause</button>'
    : '<button class="ghost" data-act="resume" title="Continue this task in the same workspace">▶ resume</button>';
  el.innerHTML = `<div id="phead"></div>
    ${c.att.length ? `<h2>Needs you</h2>${c.att.map(a => `<div class="att" data-att="${esc(a.id)}"><div class="sub">${esc(a.reason || a.kind)} · ${esc(a.task_id)}</div><div class="q">${esc(a.question)}</div>
      ${(a.options || []).length ? `<div class="row">${a.options.map(o => `<button class="ghost" data-opt="${esc(o)}">${esc(o)}</button>`).join("")}</div>` : ""}
      <textarea rows="2" placeholder="Your answer (empty = ${esc(a.default || "the default")})"></textarea><div class="row"><button data-answer>Answer</button></div><div class="err"></div></div>`).join("")}` : ""}
    ${c.alerts && c.alerts.length ? `<h2>To check <button class="ghost" data-ackall style="float:right;width:auto;padding:1px 8px;font-size:11px">dismiss all</button></h2>${c.alerts.map(a => `<div class="att al ${esc(a.kind)}" data-al="${esc(a.id)}"><div class="sub">${esc({done: "task finished", error: "error", auto_confirmed: "auto-answered"}[a.kind] || a.kind)} · ${esc(a.task_id)}</div><div class="q">${esc(short(a.text, 300))}</div>
      <div class="row"><button class="ghost" data-altask="${esc(a.task_id)}">show task</button><button data-dismiss>Dismiss</button></div></div>`).join("")}` : ""}
    <h2>Tasks of ${esc(shown(c))}</h2>
    <div class="tasks">${tasks.slice(0, 25).map(t => `<div class="task${t.id === S.panelTask ? " sel" : ""}" data-task="${esc(t.id)}"><span class="chip ${esc(t.status)}">${esc(t.status.replace("_", " "))}</span><span class="n" title="${esc(t.name)}">${esc(t.name)}</span></div>`).join("") || '<div class="hint">No tasks yet.</div>'}</div>
    ${pt ? `<div class="box"><div class="sub" title="${esc(pt.name)}">${esc(short(pt.name, 80))}</div>
      <div class="row tabs"><button class="ghost${S.chatKind === "ask" ? " on" : ""}" data-kind="ask">Ask</button><button class="ghost${S.chatKind === "steer" ? " on" : ""}" data-kind="steer">Steer</button>
        <span class="spacer"></span>${ctl}
        <a class="ghost" href="${esc(c.src.dash(pt.root_id))}" target="_blank" rel="noopener">open in dashboard ↗</a></div>
      <textarea id="chattext" rows="3" placeholder="${S.chatKind === "ask" ? `Ask ${esc(shown(c))} about this task…` : `New instructions for ${esc(shown(c))} on this task…`}"></textarea>
      <div class="row"><button id="chatsend">${S.chatKind === "ask" ? "Ask" : "Steer"}</button><span class="hint">Ctrl+Enter sends</span></div><div class="err" id="chaterr"></div></div>` : ""}
    <h2>New task for ${esc(shown(c))}</h2>
    <textarea id="newtext" rows="3" placeholder="Describe the task…"></textarea>
    <div class="row"><button id="newsend">Submit</button></div><div class="err" id="newerr"></div>
    <h2>Reports of ${esc(shown(c))}</h2><div class="reps" id="reps">${repsHtml(c)}</div>`;
  paintHeader(c);
  el.querySelectorAll("[data-task]").forEach(n => n.onclick = () => {S.panelTask = n.dataset.task; drawPanel(true)});
  el.querySelectorAll("[data-kind]").forEach(n => n.onclick = () => {S.chatKind = n.dataset.kind; drawPanel(true)});
  el.querySelectorAll("[data-act]").forEach(n => n.onclick = () => {
    const how = n.dataset.act;
    if (how === "resume") {n.textContent = "⏳ resuming…"; n.classList.add("busy"); bubbleNow(c.id, "▶ Resuming…", 6); hurry(c.id)}  // at once, before the server answers
    else n.textContent = "⏳ pausing…";
    act(c, () => api(c.src, `tasks/${encodeURIComponent(pt.root_id)}/${how}`, {body: "{}"}), $("chaterr"), () => {n.textContent = how === "resume" ? "▶ resume" : "⏸ pause"; n.classList.remove("busy")});
  });
  el.querySelectorAll(".att.al").forEach(box => {
    box.querySelector("[data-dismiss]").onclick = () => act(c, () => api(c.src, "alerts/ack", {body: JSON.stringify({ids: [box.dataset.al]})}), null);
    box.querySelector("[data-altask]").onclick = () => {S.panelTask = box.querySelector("[data-altask]").dataset.altask; drawPanel(true)};
  });
  const ackAll = el.querySelector("[data-ackall]");
  if (ackAll) ackAll.onclick = () => act(c, () => api(c.src, "alerts/ack", {body: JSON.stringify({agent: c.agent})}), null);
  el.querySelectorAll(".att:not(.al)").forEach(box => {
    const send = v => act(c, () => api(c.src, `attention/${encodeURIComponent(box.dataset.att)}`, {body: JSON.stringify({answer: v})}), box.querySelector(".err"));
    box.querySelectorAll("[data-opt]").forEach(b => b.onclick = () => send(b.dataset.opt));
    box.querySelector("[data-answer]").onclick = () => send(box.querySelector("textarea").value.trim());
  });
  if (pt) {
    const go = () => {const text = $("chattext").value.trim(); if (!text) return;
      act(c, () => api(c.src, `tasks/${encodeURIComponent(pt.root_id)}/chat`, {body: JSON.stringify({text, kind: S.chatKind, to: c.agent})}), $("chaterr"))};
    $("chatsend").onclick = go; keySend($("chattext"), go);
  }
  const submit = () => {const prompt = $("newtext").value.trim(); if (!prompt) return;
    act(c, async () => {const r = await api(c.src, "tasks", {body: JSON.stringify({prompt, agent: c.agent})}); S.panelTask = r.id; loadTasks()}, $("newerr"))};
  $("newsend").onclick = submit; keySend($("newtext"), submit);
  wireReps(c); loadReps(c);
}

/* ---------------- reports: the agent's last 10 Markdown reports (final answers and its report tool) ---------------- */
// an agent's avatar for the panel. Another HQ's uploaded images are not reachable from here: text and https images only
const avatarFor = (src, id) => {const a = (src.avs || {})[id]; return !a ? null : !src.remote || a.kind === "text" || /^https?:/i.test(a.url || "") ? a : null};
const avatarHtml = a => !a ? "" : a.kind === "image" ? `<img class="avi big" src="${esc(a.url)}" alt="">` : `<span class="avi big txt">${esc(a.text)}</span>`;
const shown = c => c.nick || c.agent;  // the panel names an agent by its nickname; the id goes below the title
const REPS = new Map();  // citizen id -> {rows, at, stale}
const REPORTS_SHOWN = 10;
const repTime = ts => /^\d{8}T\d{4}/.test(ts || "") ? `${ts.slice(0, 4)}-${ts.slice(4, 6)}-${ts.slice(6, 8)} ${ts.slice(9, 11)}:${ts.slice(11, 13)}` : ts || "";
function repsHtml(c) {
  const r = REPS.get(c.id);
  if (!r) return '<div class="hint">Loading…</div>';
  if (r.error) return `<div class="hint">${esc(r.error)}</div>`;
  return r.rows.map((x, i) => `<div class="rep" data-rep="${i}" title="${esc(x.path)}"><span class="n">${esc(x.title || "Report")}</span><span class="chip ${esc(x.status === "done" ? "working" : x.status === "error" ? "error" : "")}">${esc(x.auto ? x.status : "report")}</span>
    <span class="sub">${esc(repTime(x.timestamp))} · ${esc(short(x.root_task_name || x.root_task_id, 60))}</span></div>`).join("") || '<div class="hint">No reports yet.</div>';
}
function wireReps(c) {
  const box = $("reps"); if (!box) return;
  box.querySelectorAll("[data-rep]").forEach(n => n.onclick = () => openReport(c, REPS.get(c.id).rows[+n.dataset.rep]));
}
async function loadReps(c, force) {
  const r = REPS.get(c.id);
  if (!force && r && !r.stale && Date.now() - r.at < 15000) return;
  let next;
  try {next = {rows: (await api(c.src, `agents/${encodeURIComponent(c.agent)}/reports`)).slice(0, REPORTS_SHOWN), at: Date.now()}}
  catch (e) {next = {rows: [], at: Date.now(), error: /404/.test(e.message) && !/no agent/.test(e.message) ? "This HQ does not list reports yet." : e.message}}
  const same = r && JSON.stringify(r.rows) === JSON.stringify(next.rows) && r.error === next.error;
  REPS.set(c.id, next);
  if (!same && S.sel === c.id && $("reps")) {$("reps").innerHTML = repsHtml(c); wireReps(c)}
}
function staleReps(src, e) {  // a report may have been written: refresh that agent's list
  if (!(e.kind === "done" || (e.kind === "artifact" && (e.meta || {}).action === "report"))) return;
  const id = idOf(src, e.sender), r = id && REPS.get(id);
  if (r) {r.stale = true; if (S.sel === id) loadReps(S.cit.get(id), true)}
}
async function openReport(c, x) {
  if (!x) return;
  $("rvtitle").textContent = x.title || "Report";
  $("rvsub").textContent = `${shown(c)} · ${repTime(x.timestamp)}`;
  $("rvdash").href = c.src.dash(x.root_task_id || x.task_id);
  $("rvbody").innerHTML = '<div class="hint">Loading…</div>';
  $("rview").classList.add("on");
  let md = "";
  try {md = (await api(c.src, "reports/" + x.path.split("/").map(encodeURIComponent).join("/"))).markdown}
  catch (e) {$("rvbody").innerHTML = `<div class="err">${esc(e.message)}</div>`; return}
  $("rvbody").innerHTML = window.MD && MD.ready() ? MD.render(md) : `<pre>${esc(md)}</pre>`;
  if (window.MD && MD.enhance) MD.enhance($("rvbody"));
  $("rvdl").onclick = () => {const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([md], {type: "text/markdown"}));
    a.download = x.path.split("/").pop(); a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000)};
}
$("rvclose").onclick = () => $("rview").classList.remove("on");
$("rview").onclick = e => {if (e.target === $("rview")) $("rview").classList.remove("on")};
document.addEventListener("keydown", e => {if (e.key === "Escape" && $("rview").classList.contains("on")) $("rview").classList.remove("on")});
function paintHeader(c) {
  const st = stateOf(c), h = $("phead"); if (!h) return;
  h.innerHTML = `<h3>${esc(shown(c))} <span class="chip ${esc(st)}">${esc(st === "error" && c.att.length ? "needs you" : st)}</span><span class="spacer" style="flex:1"></span>${avatarHtml(c.av)}<button class="ghost" id="pclose" title="Close">✕</button></h3>
    ${c.nick ? `<div class="sub agentid" title="agent id">${esc(c.agent)}</div>` : ""}<div class="sub">${esc(WORLD === "office" ? (c.dept === "hq" ? S.hqName || "Headquarters" : c.dept ? c.dept + " department" : S.hqName || "Team") : c.src.title || c.src.name)}${c.busy ? ` · ${c.busy} task${c.busy > 1 ? "s" : ""} running` : ""}${c.title ? ` · ${esc(c.title)}` : ""}</div>`;
  $("pclose").onclick = () => select(null);
}
const keySend = (ta, fn) => ta.addEventListener("keydown", e => {if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {e.preventDefault(); fn()}});
async function act(c, fn, errEl, undo) {
  const btns = $("panel").querySelectorAll("button"); btns.forEach(b => b.disabled = true);
  try {await fn(); if (errEl) errEl.textContent = ""; await refresh(); drawPanel(true)}
  catch (e) {if (errEl) errEl.textContent = e.message; btns.forEach(b => b.disabled = false); if (undo) undo()}
}

/* ---------------- zoom ---------------- */
function fit() {
  if (S.r) S.r.fit($("zoom").value, $("stage"));
}

/* ---------------- start ---------------- */
(async () => {
  try {
    S.info = await (await fetch("/api/office")).json();
    const views = (S.info.views && S.info.views.length) ? S.info.views : ["3d", "2d"];  // "office": {"views": [...]}
    if (!views.includes(S.mode)) S.mode = views[0];
    if (WORLD === "office") {
      const h = VIEW && S.info.hqs.find(x => x.name === VIEW);
      S.sources = [h ? source(h.name, true, h.link) : source(S.info.name, false)];
      S.title = S.info.title || S.info.name; S.hqName = S.info.hq_name || "";
      if (h) {$("dash").href = h.link + "/"; S.title = h.name; S.hqName = "";
        try {const o = await api(S.sources[0], "office"); S.title = o.title || o.name || h.name; S.hqName = o.hq_name || ""} catch {}}
      $("title").textContent = S.title;
    } else {
      S.sources = [source(S.info.name, false), ...S.info.hqs.map(h => source(h.name, true, h.link))];
      $("title").textContent = "Planners · all HQs";
    }
    document.title = WORLD === "office" ? `${S.title} · Office` : $("title").textContent;
    $("worlds").innerHTML = `<a class="ghost${WORLD === "office" && !VIEW ? " on" : ""}" href="/office">🏢 office</a> <a class="ghost${WORLD === "planners" ? " on" : ""}" href="/office?world=planners" title="The planners of every HQ in office.hqs, in one room">🤝 planners</a>`;
    await loadLayout();
    editor.init({S, $, esc, api, build, refresh, select, store, WORLD, VIEW, loadLayout, mergeCatalog});
    viewButtons();
    $("zoom").value = store.get("office.zoom", "fit");
    $("zoom").onchange = () => {store.set("office.zoom", $("zoom").value); fit()};
    $("taskf").onchange = () => setTask($("taskf").value);
    addEventListener("resize", debounce(() => S.r && S.r.resize(), 100));
    await build();
    await loadTasks();
    const saved = store.get("office.task", ""); if (saved && S.tasks.has(saved)) {$("taskf").value = saved; await setTask(saved)}
    await refresh();
    for (const src of S.sources) {
      const es = new EventSource(src.events);
      es.onmessage = m => {try {onEvent(src, JSON.parse(m.data))} catch {}};
      es.addEventListener("relay", m => {try {src.up = JSON.parse(m.data).up; status()} catch {}});
      es.onerror = () => {if (!src.remote) {src.up = false; status()}};
      es.onopen = () => {if (!src.remote) {src.up = true; refreshSoon()}};
    }
    setInterval(refresh, 3000);
    setInterval(saveCam, 1000); addEventListener("pagehide", saveCam);
    setInterval(nextBubbles, 500);
    setInterval(loadTasks, 15000);
    if (WORLD === "planners") setInterval(() => {if (S.sources.some(s => s.up && !s.planners.length)) rebuildSoon()}, 20000);
    else setInterval(editor.watch, 10000);  // the office was edited in another tab (or by someone else): show it
  } catch (e) {
    $("empty").textContent = `Could not load the office: ${e.message}`; console.error(e);
  }
})();
