/* Claude SDK usage in the dashboard and office headers: the total pill and the plan limit chip (GET /api/usage). */
(function () {
  const esc = s => String(s).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const tok = n => n >= 1e9 ? (n / 1e9).toFixed(2) + " B" : n >= 1e6 ? (n / 1e6).toFixed(2) + " M" : n >= 1e4 ? Math.round(n / 1e3) + " k" : String(Math.round(n));
  const NAMES = {five_hour: "5h", seven_day: "7d", seven_day_opus: "7d Opus", seven_day_sonnet: "7d Sonnet", overage: "extra usage", plan: "plan"};
  const RANK = {allowed: 0, allowed_warning: 1, rejected: 2};
  const when = s => {const d = new Date(s * 1000), today = d.toDateString() === new Date().toDateString();
    return today ? d.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}) : d.toLocaleString([], {weekday: "short", hour: "2-digit", minute: "2-digit"})};
  const pct = l => l.utilization == null ? "" : ` ${Math.round(l.utilization * 100)}%`;
  const line = l => `${NAMES[l.type] || l.type}${pct(l)} · ${l.status === "rejected" ? "limit hit" : l.status === "allowed_warning" ? "near limit" : "ok"}${l.resets_at ? ` · resets ${when(l.resets_at)}` : ""}`;
  // reports: the /api/usage answers of one or more xqos (the planners world sums several)
  function render(reports, totalEl, limitEl, scope) {
    const u = {tokens: 0, cost_usd: 0, input_tokens: 0, output_tokens: 0, cache_read_input_tokens: 0, cache_creation_input_tokens: 0}, since = [], lims = [];
    let any = false;
    for (const r of reports) if (r) {any = true; for (const k in u) u[k] += +r[k] || 0; if (r.since) since.push(r.since);
      const now = Date.now() / 1000; for (const l of r.rate_limits || []) if (!(l.resets_at && l.resets_at < now)) lims.push(l)}
    if (totalEl) {
      totalEl.innerHTML = any ? `Σ ${tok(u.tokens)} tokens · <b>$${u.cost_usd.toFixed(2)}</b> <span class="est">est.</span>` : "";
      totalEl.title = `Claude usage ${scope || "of this HQ"}${since.length ? ` since ${since.sort()[0].slice(0, 10)}` : ""}. `
        + `The cost is an estimate at API prices (on a Claude subscription it is not what you pay).\n`
        + `${u.input_tokens.toLocaleString()} input · ${u.output_tokens.toLocaleString()} output · ${u.cache_read_input_tokens.toLocaleString()} cache read · ${u.cache_creation_input_tokens.toLocaleString()} cache write tokens`;
    }
    if (limitEl) {  // the tightest window on the chip, every window in the tooltip; hidden until Claude Code reports one
      lims.sort((a, b) => (RANK[b.status] - RANK[a.status]) || ((b.utilization || 0) - (a.utilization || 0)));
      const top = lims[0];
      limitEl.className = top ? (top.status === "rejected" ? "bad" : top.status === "allowed_warning" ? "warn" : "") : "";
      limitEl.textContent = top ? `${NAMES[top.type] || top.type}${pct(top) || (top.status === "rejected" ? " limit hit" : top.status === "allowed_warning" ? " near limit" : " ok")}${top.resets_at ? ` · resets ${when(top.resets_at)}` : ""}` : "";
      limitEl.title = top ? "Claude plan limits (last state Claude Code reported):\n" + lims.map(line).join("\n") : "";
    }
  }
  window.usageHeader = {render, tok, esc};
})();
