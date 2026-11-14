/* Office view, 2D renderer: the flat pixel office drawn by miniverse. Kept as the fallback for the 3D view.
   It only draws what office.js decides; clicks go back through ctx.onPick / ctx.onZone. The edited layout shows here
   too: props without a 2D picture (and uploaded models) are drawn as tinted boxes, and turned ones are drawn unturned. */
import {Miniverse, PropSystem} from "/plugins/office/vendor/miniverse/miniverse.js";
import {entry, desksOf, deskAgent} from "/plugins/office/officelayout.js";
import {IdleDirector, ACTS as IDLE_ACTS, CHAT} from "/plugins/office/officeidle.js";

const T = 32, ART = "/plugins/office/office";
const SPRITES = ["nova", "rio", "dexter", "morty"];

function charSprites(name) {
  const a = {sheet: "walk", row: 0, frames: 2, speed: 0.5};
  const st = (sheet, row, frames, speed) => ({sheet, row, frames, speed});
  return {sheets: {walk: `${ART}/citizens/${name}_walk.png`, actions: `${ART}/citizens/${name}_actions.png`,
      idle: `${ART}/citizens/${name}_idle.png`, stand: `${ART}/citizens/${name}_stand.png`},  // idle/stand: placeholders (tools/make_placeholder_sprites.py)
    animations: {idle_down: a, idle_up: {...a, row: 1},
      walk_down: {sheet: "walk", row: 0, frames: 4, speed: 0.15}, walk_up: {sheet: "walk", row: 1, frames: 4, speed: 0.15},
      walk_left: {sheet: "walk", row: 2, frames: 4, speed: 0.15}, walk_right: {sheet: "walk", row: 3, frames: 4, speed: 0.15},
      working: {sheet: "actions", row: 0, frames: 4, speed: 0.3}, sleeping: {sheet: "actions", row: 1, frames: 2, speed: 0.8},
      talking: {sheet: "actions", row: 2, frames: 4, speed: 0.15},
      read: st("idle", 0, 4, 0.9), drink: st("idle", 1, 4, 0.7), tend: st("idle", 2, 4, 0.3), write: st("idle", 3, 4, 0.25),
      idle_left: st("stand", 0, 2, 0.5), idle_right: st("stand", 1, 2, 0.5), sit_down: st("stand", 2, 2, 0.8), sit_up: st("stand", 3, 2, 0.8)},
    frameWidth: 64, frameHeight: 64};
}

/* an agent called back by a click on its monitor (or desk) sits at its desk: miniverse seats only a working citizen there
   (the screen still shows what office.js says, so it is not drawn as working) */
function statuses(ctx) {
  const list = ctx.statuses();
  if (!ctx.recalled) return list;
  return list.map(s => ctx.recalled(ctx.S.cit.get(s.id)) && (s.state === "idle" || s.state === "sleeping") ? {...s, state: "working", task: null} : s);
}

/* ctx: {el, L, S, colors, statuses, onPick(id), onMonitor(id), onZone(zone), cat, characters} */
export async function mount(ctx) {
  const {el, L, S} = ctx;
  const walkable = L.floor.map((row, r) => row.map((_, c) => !L.walls.has(`${c},${r}`)));
  const tiles = Object.fromEntries(["main_floor", "concrete_accent", "exposed_brick_wall"].map(k => [k, `${ART}/tiles/${k}.png`]));
  const mv = new Miniverse({container: el, world: "hq", scene: "main", autoSpawn: false,
    signal: {type: "mock", interval: 600, mockData: () => statuses(ctx)},
    citizens: [...S.cit.values()].map(c => {const k = ctx.characters.indexOf((L.characters || {})[c.id]);
      return {agentId: c.id, name: c.name, sprite: SPRITES[(k >= 0 ? k : c.look) % SPRITES.length], position: c.home}}),
    spriteSheets: Object.fromEntries(SPRITES.map(s => [s, charSprites(s)])),
    scale: 1, width: L.cols * T, height: L.rows * T,
    sceneConfig: {name: "main", tileWidth: T, tileHeight: T, layers: [L.floor], walkable, locations: {}, tiles}});
  const props = new PropSystem(T, 1);
  // a picture is drawn unturned, centred where the prop is; its spots stay where the layout put them
  const pieces = [], boxes = [];
  for (const p of L.props) {
    const e = entry(ctx.cat, p.id);
    if (e.marker) continue;  // a waypoint: only its spot counts (setWanderPoints below)
    if (!e.sprite2d) {boxes.push({p, color: e.color || (e.missing ? "#d53f8c" : "#8a94a3")}); continue}
    const x = p.x + (p.w - p.w0) / 2, y = p.y + (p.h - p.h0) / 2;
    pieces.push({id: e.sprite2d, x, y, w: p.w0, h: p.h0, layer: p.layer,
      anchors: p.anchors.length ? p.anchors.map(a => ({name: a.name, type: a.type, ox: p.x + a.ox - x, oy: p.y + a.oy - y})) : undefined});
  }
  await Promise.all([...new Set(pieces.map(p => p.id))].map(id => props.loadSprite(id, `${ART}/props/${id}.png`).catch(() => {})));
  props.setLayout(pieces);
  props.setWanderPoints(L.wander);
  const spots = props.getLocations();  // spots of boxed props too: the agents still use them
  for (const {p} of boxes) for (const a of p.anchors) spots.push({name: a.name, x: Math.round(p.x + a.ox), y: Math.round(p.y + a.oy), type: a.type});
  mv.setTypedLocations(spots);
  const blocked = props.getBlockedTiles(); for (const w of L.walls) blocked.add(w);
  for (const k of L.paint.block) blocked.add(k);  // no-go paint (corridors are 3D only)
  for (const {p} of boxes) if (p.blocks) for (let r = Math.floor(p.y + 0.01); r < Math.ceil(p.y + p.h - 0.01); r++) for (let c = Math.floor(p.x + 0.01); c < Math.ceil(p.x + p.w - 0.01); c++) blocked.add(`${c},${r}`);
  mv.updateWalkability(blocked);
  await mv.start();
  const idle = idleOffice(mv, L, ctx);
  mv.addLayer({order: 1, render: c => drawZones(c, S, ctx.colors)});
  mv.addLayer({order: 5, render: c => {props.renderBelow(c); drawBoxes(c, boxes)}});
  const screens = screensOf(L, ctx);
  mv.addLayer({order: 6, render: (c, dt) => drawScreens(c, screens, S, ctx.stateOf, dt, ctx.alertOf)});
  mv.addLayer({order: 15, render: c => props.renderAbove(c)});
  const tags = mv.renderer.layers.find(l => l.order === 30);  // miniverse's 8px name tags: drawn bigger in drawBadges
  if (tags) mv.renderer.removeLayer(tags);
  mv.addLayer({order: 31, render: c => drawBadges(c, mv, S, ctx)});
  mv.addLayer({order: 33, render: c => idle.draw(c)});  // 📖 ☕ 💬 ... above an agent while it pretends
  // speech bubbles: wrapped on up to 4 lines (miniverse draws one line of 20 characters), at least 5 seconds
  const sb = mv.speechBubbles, show = sb.show.bind(sb);
  sb.show = (x, y, text, secs = 5, target) => show(x, y, text, Math.max(5, secs), target);
  sb.render = (c, dt) => drawBubbles(c, sb, dt);
  sb.order = 32; mv.renderer.layers.sort((a, b) => a.order - b.order);  // over the name tags
  let last = null;
  const size = {w: L.cols * T, h: L.rows * T}, canvas = mv.getCanvas();

  const worldXY = e => {const r = canvas.getBoundingClientRect();
    return {x: (e.clientX - r.left) * canvas.width / r.width, y: (e.clientY - r.top) * canvas.height / r.height}};
  const hit = e => {const p = worldXY(e); return mv.getCitizens().find(c => c.visible && c.containsPoint(p.x, p.y)) || null};
  const zoneAt = e => {if (!ctx.zoneLinks) return null; const p = worldXY(e);
    return S.zones.find(z => z.tag && p.x >= z.tag.x && p.x <= z.tag.x + z.tag.w && p.y >= z.tag.y && p.y <= z.tag.y + z.tag.h) || null};
  // an agent's desk: its monitor, the desk (also one without a computer) or its chair; turned props by their footprint
  const desks = desksOf(L, [...S.cit.keys()]);
  const deskAt = e => {const p = worldXY(e), x = p.x / T, y = p.y / T;
    for (const q of L.props) {
      const uid = q.parent || q.uid;
      if (desks.has(uid) && x >= q.x && x <= q.x + q.w && y >= q.y && y <= q.y + q.h) return deskAgent(desks, uid, x, y);
    }
    return null};
  canvas.addEventListener("click", e => {const c = hit(e); if (c) return ctx.onPick(c.agentId);
    const d = deskAt(e); if (d) return (ctx.onMonitor || ctx.onPick)(d); const z = zoneAt(e); if (z) ctx.onZone(z)});
  canvas.addEventListener("mousemove", e => {canvas.style.cursor = hit(e) || deskAt(e) || zoneAt(e) ? "pointer" : "default"});
  const seatOf = id => spots.find(s => s.name === (S.cit.get(id) || {}).home);

  return {
    kind: "2d", mv, screens, idle: idle.director, idleAnim: idle.anim,
    stop() {mv.stop(); el.innerHTML = ""},
    message(from, to) {mv.signal.emitEvent({id: ++S.ev, timestamp: Date.now(), agentId: from, action: {type: "message", to}})},
    bubble(id, text, secs) {const r = mv.getCitizen(id); if (r && r.visible && text) mv.speechBubbles.show(r.x + 16, r.y - 8, text, secs, r)},
    fit(zoom, stage) {
      last = [zoom, stage];
      let k = zoom === "fit" ? Math.min((stage.clientWidth - 2) / size.w, (stage.clientHeight - 2) / size.h, 2) : Number(zoom);
      k = Math.max(.25, k);
      canvas.style.width = `${Math.round(size.w * k)}px`; canvas.style.height = `${Math.round(size.h * k)}px`;
    },
    resize() {if (last) this.fit(...last)},
    recall() {},  // the next status poll (600 ms) seats it: see statuses()
    atDesk(id) {const r = mv.getCitizen(id), s = seatOf(id); if (!r || !s || r.isMoving()) return false; const t = r.getTilePosition(); return t.x === s.x && t.y === s.y},
    deskPos(id) {  // where its monitor (or its desk) is on the page, for tests
      const R = canvas.getBoundingClientRect(), k = R.width / canvas.width, sc = screens.find(s => s.id === id);
      if (sc) return {x: R.left + (sc.x + sc.w / 2) * k, y: R.top + (sc.y + sc.h / 2) * k, monitor: true};
      const d = [...desks].find(([, v]) => v.some(s => s.id === id)), p = d && L.byUid.get(d[0]);
      return p ? {x: R.left + (p.x + p.w / 2) * T * k, y: R.top + (p.y + p.h * 0.3) * T * k, monitor: false} : null;
    },
    screenPos(id) {const r = mv.getCitizen(id), R = canvas.getBoundingClientRect(); if (!r) return null;
      return {x: R.left + (r.x + 16) * R.width / canvas.width, y: R.top + (r.y + 10) * R.height / canvas.height}},
  };
}

/* Idle agents read at a shelf, drink at the bar, sit on the sofa, write on the whiteboard, tend a plant, or gather and
   chat (officeidle.js). The library wanders on its own; its idle step is replaced per citizen, everything else (working,
   walking, name tags) stays the library's. An agent that gets work leaves its spot at once. */
function idleOffice(mv, L, ctx) {
  const spots = [];
  for (const p of L.props) for (const a of p.anchors || []) if (a.act) {
    spots.push({name: a.name, act: a.act, prop: a.prop, x: Math.round(p.x + a.ox), y: Math.round(p.y + a.oy), fx: a.fx, fy: a.fy, rot: a.rot || 0});
  }
  // which way an agent at a spot looks: at the prop (or at the others of its chat group); sideways uses idle_left / idle_right,
  // up and down the act's own animation (read, drink, tend: front; write: back; sit: front, or back on a sofa turned 180)
  const side = (s, tx, ty) => {
    const dx = tx - (s.x + 0.5), dy = ty - (s.y + 0.5);
    return Math.abs(dx) > Math.abs(dy) + 0.25 ? (dx < 0 ? "idle_left" : "idle_right") : null;
  };
  const animFor = (P, now) => {
    const s = P.spot, chat = P.act === "chat", kind = chat ? P.base : P.act;
    if (kind === "sit") return s.rot === 180 ? "sit_up" : "sit_down";
    if (chat) {
      const mates = (P.crew || []).map(m => D.plans.get(m)).filter(m => m && m.spot !== s);
      const f = mates.length ? side(s, mates.reduce((t, m) => t + m.spot.x + 0.5, 0) / mates.length, mates.reduce((t, m) => t + m.spot.y + 0.5, 0) / mates.length) : null;
      return f && Math.floor(now / 2600) % 2 ? f : CHAT.anim;  // listeners look at the others, then speak
    }
    return side(s, s.fx, s.fy) || IDLE_ACTS[kind].anim;
  };
  const D = new IdleDirector(spots, ctx.idle || {}, Math.random), icons = new Map(), tries = new Map();
  const cits = () => mv.getCitizens();
  const idleIds = () => cits().filter(c => c.state === "idle").map(c => c.agentId);
  const settle = c => {icons.delete(c.agentId); tries.delete(c.agentId); if (c.animator.getCurrentAnimation() !== "idle_down" && !c.isMoving()) c.animator.play("idle_down")};
  for (const cit of cits()) {
    const id = cit.agentId, orig = cit.updateIdleBehavior.bind(cit), upd = cit.update.bind(cit);
    cit.update = (...a) => {  // work (or any state but idle) ends the pretending
      if (cit.state !== "idle" && D.plans.has(id)) {D.release(id); icons.delete(id); tries.delete(id)}
      return upd(...a);
    };
    cit.updateIdleBehavior = (dt, pf, locs, typed, resv, homes) => {
      const now = Date.now();
      if (!D.enabled) return orig(dt, pf, locs, typed, resv, homes);
      const had = D.plans.has(id), P = D.current(id, now) || D.plan(id, {idle: idleIds(), total: cits().length,
        free: s => !!resv && resv.isAvailable(s.x, s.y, id)}, now);
      if (!P) {if (had) settle(cit); return orig(dt, pf, locs, typed, resv, homes)}  // nothing to do: the library's own wandering
      const s = P.spot, tp = cit.getTilePosition(), kind = P.act === "chat" ? P.base : P.act, def = IDLE_ACTS[kind];
      if (tp.x !== s.x || tp.y !== s.y) {  // not there yet (a path ended elsewhere, or it has not started): go
        icons.delete(id);
        const n = (tries.get(id) || 0) + 1; tries.set(id, n);
        if (n > 3 || !cit.goToAnchor(s.name, typed, pf, resv)) {D.release(id); settle(cit)}
        return;
      }
      tries.delete(id); D.arrive(id, now);  // the stay counts from arrival
      const anim = animFor(P, now);
      if (cit.animator.getCurrentAnimation() !== anim) cit.animator.play(anim);
      icons.set(id, P.act === "chat" ? CHAT.icon : def.icon);
    };
  }
  return {director: D, anim: animFor, draw(c) {
    c.font = "14px system-ui, sans-serif"; c.textAlign = "center";
    for (const [id, icon] of icons) {
      const r = mv.getCitizen(id); if (!r || !r.visible) continue;
      c.fillText(icon, r.x + T / 2, r.y - r.spriteSheet.config.frameHeight + T - 18 - r.getSittingOffset());
    }
    c.textAlign = "start";
  }};
}

function drawBoxes(ctx, boxes) {
  for (const {p, color} of boxes) {
    ctx.globalAlpha = .55; ctx.fillStyle = color; ctx.fillRect(p.x * T + 2, p.y * T + 2, p.w * T - 4, p.h * T - 4);
    ctx.globalAlpha = .9; ctx.strokeStyle = color; ctx.lineWidth = 1; ctx.strokeRect(p.x * T + 2.5, p.y * T + 2.5, p.w * T - 5, p.h * T - 5);
  }
  ctx.globalAlpha = 1;
}
function drawZones(ctx, S, COLORS) {
  ctx.font = "bold 11px system-ui, sans-serif";
  S.zones.forEach((z, i) => {
    const col = COLORS[i % COLORS.length], X = z.x * T, Y = z.y * T, W = z.w * T, H = z.h * T;
    ctx.globalAlpha = .10; ctx.fillStyle = col; ctx.fillRect(X, Y, W, H);
    ctx.globalAlpha = .55; ctx.strokeStyle = col; ctx.lineWidth = 2; ctx.strokeRect(X + 1, Y + 1, W - 2, H - 2);
    ctx.globalAlpha = 1;
    const label = z.label + (z.down ? " · offline" : ""), tw = ctx.measureText(label).width + 12;
    z.tag = {x: X + 4, y: Y - 9, w: tw, h: 16};
    ctx.fillStyle = col; ctx.beginPath(); ctx.roundRect ? ctx.roundRect(X + 4, Y - 9, tw, 16, 8) : ctx.rect(X + 4, Y - 9, tw, 16); ctx.fill();
    ctx.fillStyle = "#0b0d12"; ctx.fillText(label, X + 10, Y + 3);
  });
}
/* the screens of the desks with a computer: scrolling code while their agent works (the sprite's screen area) */
const SCREEN = {x0: 0.39, x1: 0.61, y0: 0.15, y1: 0.3}, INK = ["#7fc4ff", "#9ae6b4", "#fbd38d", "#f6a5c0", "#cbd5e0"];
function screensOf(L, ctx) {
  const out = [];
  for (const c of ctx.S.cit.values()) {
    const p = L.props.find(p => entry(ctx.cat, p.id).builtin === "desk" && entry(ctx.cat, p.id).sprite2d && p.anchors.some(a => a.name === c.home));
    if (!p) continue;
    const x = (p.x + (p.w - p.w0) / 2) * T, y = (p.y + (p.h - p.h0) / 2) * T, w = p.w0 * T, h = p.h0 * T;
    out.push({id: c.id, x: x + SCREEN.x0 * w, y: y + SCREEN.y0 * h, w: (SCREEN.x1 - SCREEN.x0) * w, h: (SCREEN.y1 - SCREEN.y0) * h, off: Math.random() * 40, speed: 6, next: 0, blink: 0,
      lines: Array.from({length: 14}, () => ({indent: Math.floor(Math.random() * 4) * 2, w: 3 + Math.random() * 12, ink: INK[Math.floor(Math.random() * INK.length)], empty: Math.random() < 0.15}))});
  }
  return out;
}
const FLASH = {attention: "#ff5d6c", error: "#ff5d6c", auto_confirmed: "#f6ad55", done: "#38a169"};
function drawScreens(g, screens, S, stateOf, dt, alertOf) {
  const now = Date.now();
  for (const s of screens) {
    const c = S.cit.get(s.id), st = c && stateOf(c);
    const need = c && st !== "offline" && alertOf ? alertOf(c) : null;
    if (need) {  // something needs the user: the monitor flashes until it is answered / dismissed (alerts.py)
      const on = Math.floor(now / 450) % 2 === 0;
      g.save(); g.fillStyle = on ? FLASH[need] || FLASH.attention : "#0d1726"; g.fillRect(s.x, s.y, s.w, s.h);
      if (on) {g.fillStyle = "#fff"; g.font = `bold ${Math.max(6, Math.floor(s.h * 0.8))}px system-ui, sans-serif`; g.textAlign = "center"; g.textBaseline = "middle";
        g.fillText(need === "done" ? "✓" : "!", s.x + s.w / 2, s.y + s.h / 2 + 0.5)}
      g.restore();
      continue;
    }
    if (st !== "working" && st !== "error") continue;
    if (now > s.next) {s.speed = Math.random() < 0.2 ? 0 : 3 + Math.random() * 12; s.blink = Math.random() < 0.3 ? now + 60 + Math.random() * 140 : 0; s.next = now + 700 + Math.random() * 2500}
    s.off = (s.off + s.speed * (dt || 0)) % (s.lines.length * 3);
    g.save(); g.beginPath(); g.rect(s.x, s.y, s.w, s.h); g.clip();
    g.fillStyle = st === "error" ? "#5a1820" : "#0d1726"; g.fillRect(s.x, s.y, s.w, s.h);
    g.globalAlpha = s.blink > now ? 0.35 : 1;
    for (let k = 0; k < s.lines.length * 2; k++) {
      const l = s.lines[k % s.lines.length], y = s.y + 1 + k * 3 - s.off;
      if (l.empty || y < s.y - 2 || y > s.y + s.h) continue;
      g.fillStyle = st === "error" ? "#ff8a95" : l.ink; g.fillRect(s.x + 1 + l.indent, y, Math.min(l.w, s.w - 2 - l.indent), 1.5);
    }
    g.restore();
  }
}
function wrap(g, text, max, lines) {
  const out = []; let cur = "";
  for (let word of String(text).split(/\s+/).filter(Boolean)) {
    while (g.measureText(word).width > max) {  // a word longer than a line: split it
      let k = word.length; while (k > 1 && g.measureText(word.slice(0, k)).width > max) k--;
      if (cur) {out.push(cur); cur = ""}
      out.push(word.slice(0, k)); word = word.slice(k);
    }
    const next = cur ? cur + " " + word : word;
    if (g.measureText(next).width > max && cur) {out.push(cur); cur = word} else cur = next;
  }
  if (cur) out.push(cur);
  if (out.length > lines) {out.length = lines; let last = out[lines - 1] + "…"; while (g.measureText(last).width > max && last.length > 2) last = last.slice(0, -2) + "…"; out[lines - 1] = last}
  return out;
}
function drawBubbles(g, sb, dt) {
  for (const b of sb.bubbles) b.life -= dt;
  sb.bubbles = sb.bubbles.filter(b => b.life > 0);
  for (const b of sb.bubbles) {
    if (b.target) {b.x = b.target.x + b.offsetX; b.y = b.target.y + b.offsetY - b.target.getSittingOffset()}
    g.save(); g.globalAlpha = Math.min(1, b.life / 0.5); g.font = "10px system-ui, sans-serif";
    const lines = wrap(g, b.text, 150, 4), w = Math.max(...lines.map(l => g.measureText(l).width)) + 12, h = lines.length * 12 + 8;
    const x = b.x - w / 2, y = b.y - h - 46;  // above the name tag
    g.fillStyle = "#fff"; g.strokeStyle = "#333"; g.lineWidth = 1;
    g.beginPath(); g.roundRect ? g.roundRect(x, y, w, h, 4) : g.rect(x, y, w, h); g.fill(); g.stroke();
    g.beginPath(); g.moveTo(b.x - 4, y + h); g.lineTo(b.x, y + h + 6); g.lineTo(b.x + 4, y + h); g.fill();
    g.fillStyle = "#222"; lines.forEach((l, i) => g.fillText(l, x + 6, y + 13 + i * 12));
    g.restore();
  }
}
function drawBadges(ctx, mv, S, o) {
  for (const r of mv.getCitizens()) {
    if (!r.visible) continue;
    const c = S.cit.get(r.agentId); if (!c) continue;
    const top = r.y - r.spriteSheet.config.frameHeight + T - 4 - r.getSittingOffset(), mx = r.x + T / 2;
    ctx.font = "bold 11px system-ui, sans-serif";
    const label = r.name, w = ctx.measureText(label).width + 8;
    ctx.fillStyle = S.sel === c.id ? "rgba(122,162,255,.9)" : "rgba(0,0,0,.65)"; ctx.fillRect(mx - w / 2, top - 11, w, 14);
    ctx.fillStyle = S.sel === c.id ? "#0b0d12" : "#fff"; ctx.textAlign = "center"; ctx.fillText(label, mx, top); ctx.textAlign = "start";
    if (S.sel === c.id) {
      ctx.strokeStyle = "#7aa2ff"; ctx.lineWidth = 2; ctx.beginPath(); ctx.ellipse(mx, r.y + T - 3, 15, 6, 0, 0, Math.PI * 2); ctx.stroke();
    }
    const done = o.doneOf ? o.doneOf(c) : [], ok = done.every(x => x.ok);  // finished tasks you have not looked at
    if (c.att.length || done.length) {
      ctx.globalAlpha = 0.45 + 0.55 * Math.abs(Math.sin(performance.now() / 260));  // blinks until dealt with (alerts.py)
      ctx.fillStyle = c.att.length ? "#ff5d6c" : ok ? "#38a169" : "#e53e3e"; const bx = mx + w / 2 + 10, by = top - 4;  // right of the name tag: the flashing monitor stays visible
      ctx.beginPath(); ctx.arc(bx, by, 8, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = "#fff"; ctx.font = "bold 11px system-ui, sans-serif"; ctx.textAlign = "center";
      ctx.fillText(c.att.length ? (c.att.length > 1 ? String(c.att.length) : "!") : (ok ? "✓" : "✗") + (done.length > 1 ? done.length : ""), bx, by + 4); ctx.textAlign = "start";
      ctx.globalAlpha = 1;
    }
  }
}
