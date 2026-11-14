/* What idle agents do in the office, shared by the 2D and the 3D view (no DOM, no three.js, so it is testable).

   A spot (anchor) may carry an `act`: read (bookshelf), drink (bar, fridge), sit (sofa), write (whiteboard), tend (plant).
   An idle agent picks a free spot, walks there, stays for a while, and is released the moment it has work again.
   Several idle agents may gather at one prop and chat (the same spots, a speech bubble and a talking pose).
   The director only decides; the views walk, pose and draw. Settings: "office": {"idle": {...}} (view.py `_idle`). */

export const ACTS = {
  read:  {icon: "📖", secs: [18, 45], weight: 3, pose: "holding-both",  anim: "read"},
  drink: {icon: "☕", secs: [10, 22], weight: 3, pose: "holding-right", anim: "drink"},
  sit:   {icon: "🛋", secs: [20, 50], weight: 3, pose: "sit",           anim: "sit_down"},
  write: {icon: "✎",  secs: [12, 30], weight: 2, pose: "interact-right", anim: "write"},
  tend:  {icon: "💧", secs: [8, 18],  weight: 2, pose: "crouch",        anim: "tend"},
};
export const CHAT = {icon: "💬", secs: [12, 28], pose: "emote-yes", anim: "talking", groupMax: 4, chance: 0.35};
export const WALK_MAX = 120;  // seconds an agent may take to get there before it gives up (an unreachable spot)
export const DEFAULTS = {enabled: true, activities: Object.keys(ACTS), chat: true, max_away: 0.6};

export class IdleDirector {
  /* spots: [{name, act, prop, ...}] (only those with an act count); cfg: office.idle; rng: () => [0,1) (seed it in tests) */
  constructor(spots, cfg = {}, rng = Math.random) {
    this.cfg = {...DEFAULTS, ...cfg};
    this.rng = rng;
    this.spots = (spots || []).filter(s => s.act && ACTS[s.act] && this.cfg.activities.includes(s.act));
    this.plans = new Map();   // agent id -> {act, spot, until, chat, group}
    this.last = new Map();    // agent id -> its last act (not repeated at once)
    this.groups = 0;
  }
  get enabled() {return !!this.cfg.enabled && this.spots.length > 0}
  rand(lo, hi) {return lo + this.rng() * (hi - lo)}
  taken(name, except) {for (const [id, p] of this.plans) if (id !== except && p.spot.name === name) return true; return false}
  // the plan an agent is on, until it ends (then null: ask `plan` again)
  current(id, now) {
    const p = this.plans.get(id);
    if (p && now > p.until) {this.release(id); return null}
    return p || null;
  }
  release(id) {this.plans.delete(id)}
  // the agent is at its spot: the time to stay counts from now, not from when it set off across the office
  arrive(id, now) {const p = this.plans.get(id); if (p && !p.at) {p.at = now; p.until = now + p.dur}}
  away() {return this.plans.size}
  /* pick something for `id`. idle: ids of all idle agents (chat partners come from them); total: all agents in the office;
     free(spot) => the view can use the spot now (reachable, not claimed). Returns the plan or null. */
  plan(id, {idle = [], total = 1, free = () => true}, now) {
    if (!this.enabled) return null;
    const cur = this.current(id, now); if (cur) return cur;
    if (this.plans.size >= Math.max(1, Math.floor(total * this.cfg.max_away))) return null;  // desks must not empty out
    const ok = s => !this.taken(s.name, id) && free(s);
    // chat: gather at a prop with enough free spots
    if (this.cfg.chat && this.rng() < CHAT.chance) {
      const mates = idle.filter(m => m !== id && !this.plans.has(m));
      const g = mates.length ? this.hub(ok, 1 + Math.min(CHAT.groupMax - 1, mates.length)) : null;
      if (g) {
        const crew = [id, ...shuffle(mates, this.rng)].slice(0, g.spots.length), dur = this.rand(...CHAT.secs) * 1000, group = ++this.groups;
        crew.forEach((m, i) => this.plans.set(m, {act: "chat", base: g.spots[i].act, spot: g.spots[i], dur, until: now + WALK_MAX * 1000, group, crew}));
        this.last.set(id, "chat");
        return this.plans.get(id);
      }
    }
    // solo: a weighted pick of an act that has a free spot, not the one it just did
    const by = {};
    for (const s of this.spots) if (ok(s)) (by[s.act] ||= []).push(s);
    const acts = Object.keys(by).map(a => [a, ACTS[a].weight * (this.last.get(id) === a ? 0.2 : 1)]);
    if (!acts.length) return null;
    let r = this.rng() * acts.reduce((t, [, w]) => t + w, 0), act = acts[0][0];
    for (const [a, w] of acts) {if ((r -= w) <= 0) {act = a; break}}
    const spot = by[act][Math.floor(this.rng() * by[act].length)];
    this.last.set(id, act);
    const plan = {act, spot, dur: this.rand(...ACTS[act].secs) * 1000, until: now + WALK_MAX * 1000};
    this.plans.set(id, plan);
    return plan;
  }
  // the prop with the most free spots (at least `min`, at most groupMax) of one act, random among the best
  hub(ok, want) {
    const props = new Map();
    for (const s of this.spots) if (ok(s)) {const k = `${s.prop}|${s.act}`; (props.get(k) || props.set(k, []).get(k)).push(s)}
    const fits = [...props.values()].filter(v => v.length >= 2);
    if (!fits.length) return null;
    const top = Math.min(want, CHAT.groupMax), best = Math.max(...fits.map(v => Math.min(v.length, top)));
    const pick = fits.filter(v => Math.min(v.length, top) === best);
    return {spots: shuffle(pick[Math.floor(this.rng() * pick.length)], this.rng).slice(0, top)};
  }
}

function shuffle(a, rng) {
  const o = [...a];
  for (let i = o.length - 1; i > 0; i--) {const j = Math.floor(rng() * (i + 1)); [o[i], o[j]] = [o[j], o[i]]}
  return o;
}
