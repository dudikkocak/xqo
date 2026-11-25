// Optional login (auth.py): a 401 from any API call goes to /login, and the header shows who is signed in with
// Sign out and Link Google / Link Telegram. Does nothing when login is off.
(() => {
  const toLogin = () => location.replace("/login?next=" + encodeURIComponent(location.pathname + location.search));
  const origFetch = window.fetch.bind(window);
  window.fetch = (...args) => origFetch(...args).then(r => {
    if (r.status === 401 && r.headers.get("WWW-Authenticate") === "Bearer") toLogin();
    return r;
  });
  window.xqoAuthCheck = () => origFetch("/api/auth/me").then(r => r.json()).then(me => {  // e.g. after the event stream drops
    if (me.enabled && !me.user) toLogin();
    return me;
  });

  const css = `#authchip{position:relative;flex:none;font-size:12px}#authchip>summary{list-style:none;cursor:pointer;border:1px solid var(--line,#2a2f3a);
border-radius:999px;padding:2px 10px;white-space:nowrap;max-width:200px;overflow:hidden;text-overflow:ellipsis}#authchip>summary::-webkit-details-marker{display:none}
#authchip .menu{position:fixed;right:12px;top:46px;z-index:1000;min-width:240px;background:var(--panel,#171a21);border:1px solid var(--line,#2a2f3a);
border-radius:8px;padding:8px;box-shadow:0 8px 24px rgba(0,0,0,.4)}#authchip .menu div{padding:4px 6px;color:var(--mute,#8b93a7)}
#authchip .menu button{display:block;width:100%;text-align:left;margin:2px 0;padding:5px 6px;background:none;border:0;color:var(--text,#e6e8ee);cursor:pointer;border-radius:4px;font:inherit}
#authchip .menu button:hover{background:#232838}#authchip .note{color:var(--accent,#7aa2ff);white-space:normal}#authchip .note.err{color:var(--error,#fc8181)}
#authchip .id{display:flex;justify-content:space-between;gap:6px;align-items:center}#authchip .id button{width:auto;display:inline;padding:0 4px;color:var(--mute,#8b93a7)}`;

  const el = (tag, props = {}, ...kids) => { const e = Object.assign(document.createElement(tag), props); e.append(...kids); return e; };
  const label = { google: "Google", telegram: "Telegram", token: "API token" };
  let note = new URLSearchParams(location.search).get("auth_error") || "", noteErr = !!note;

  function post(url, body) {
    return origFetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) })
      .then(r => r.json().then(d => { if (!r.ok) throw new Error(d.detail || r.status); return d; }));
  }

  function linkTelegram() {
    post("/auth/telegram/start", { link: true, next: location.pathname }).then(d => {
      window.open(d.url, "_blank", "noopener");
      note = "Open the Telegram link, tap Start, then Approve: " + d.url; noteErr = false; render();
      const until = Date.now() + d.expires_in * 1000;
      const poll = () => origFetch("/auth/telegram/status").then(r => r.json()).then(s => {
        if (s.status === "pending" && Date.now() < until) return setTimeout(poll, 1500);
        note = s.status === "approved" ? "Telegram linked." : (s.reason || "The link expired.");
        noteErr = s.status !== "approved"; refresh();
      });
      poll();
    }).catch(e => { note = String(e.message || e); noteErr = true; render(); });
  }

  let me = null;
  function render() {
    const header = document.querySelector("header"); if (!header || !me || !me.user) return;
    let chip = document.getElementById("authchip");
    const open = chip ? chip.open : !!note;
    if (chip) chip.remove();
    const u = me.user, menu = el("div", { className: "menu" });
    menu.append(el("div", { textContent: "Signed in as " + u.name }));
    for (const i of u.identities) {
      const row = el("div", { className: "id" }, el("span", { textContent: `${label[i.provider] || i.provider}: ${i.name}` }));
      if (!i.primary) row.append(el("button", { textContent: "unlink", title: "Unlink this account", onclick: () =>
        post("/api/auth/unlink", { identity: i.id }).then(refresh).catch(e => { note = e.message; noteErr = true; render(); }) }));
      menu.append(row);
    }
    for (const p of u.can_link) menu.append(el("button", { textContent: "Link " + label[p], onclick: () =>
      p === "google" ? location.href = "/auth/google/start?link=1&next=" + encodeURIComponent(location.pathname) : linkTelegram() }));
    if (note) menu.append(el("div", { className: "note" + (noteErr ? " err" : ""), textContent: note }));
    menu.append(el("button", { textContent: "Sign out", onclick: () => post("/auth/logout").then(toLogin) }));
    menu.append(el("button", { textContent: "Sign out everywhere", title: "End every session of every signed-in person",
      onclick: () => post("/auth/logout", { everywhere: true }).then(toLogin) }));
    chip = el("details", { id: "authchip", open }, el("summary", { textContent: "👤 " + u.name, title: u.via }), menu);
    header.append(chip);
  }
  function refresh() { return window.xqoAuthCheck().then(m => { me = m; render(); }); }

  document.addEventListener("DOMContentLoaded", () => {
    document.head.append(el("style", { textContent: css }));
    refresh().catch(() => {});
    if (note) history.replaceState(null, "", location.pathname);
  });
})();
