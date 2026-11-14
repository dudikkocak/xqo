/* Markdown rendering for the dashboard: markdown-it (GFM, footnotes, task lists), KaTeX math,
   Mermaid diagrams (lazy, strict), highlight.js code, DOMPurify on every result.
   API: MD.render(text) -> safe HTML string, MD.enhance(el) wires copy buttons and diagrams,
        MD.toc(el) -> table-of-contents HTML for the headings inside el. */
(function () {
  "use strict";
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const ok = () => window.markdownit && window.DOMPurify;

  /* ---------- math: $…$, $$…$$, \(…\), \[…\] ---------- */
  function tex(src, display) {
    if (!window.katex) return `<code class="math-raw">${esc(src)}</code>`;
    try {
      return katex.renderToString(src, {displayMode: display, throwOnError: true, strict: "ignore", trust: false,
                                        maxSize: 50, maxExpand: 1000, output: "htmlAndMathml"});
    } catch (e) {  // show the raw formula instead of breaking the page
      const raw = display ? `$$${src}$$` : `$${src}$`;
      return `<code class="math-err" title="${esc(e.message || e)}">${esc(raw)}</code>`;
    }
  }
  function mathPlugin(md) {
    md.inline.ruler.before("escape", "math_inline", (state, silent) => {
      const s = state.src, p = state.pos;
      let open, close, display;
      if (s.startsWith("\\(", p)) [open, close, display] = ["\\(", "\\)", false];
      else if (s.startsWith("\\[", p)) [open, close, display] = ["\\[", "\\]", true];
      else if (s.startsWith("$$", p)) [open, close, display] = ["$$", "$$", true];
      else if (s[p] === "$") [open, close, display] = ["$", "$", false];
      else return false;
      const start = p + open.length;
      let end = start;
      while ((end = s.indexOf(close, end)) !== -1) {
        if (s[end - 1] !== "\\") break;
        end += 1;
      }
      if (end === -1 || end === start) return false;
      const body = s.slice(start, end);
      if (open === "$") {  // avoid prices like "$5 and $10": no inner edge spaces, no digit right after
        if (/^\s|\s$/.test(body) || /\d/.test(s[end + 1] || "") || body.includes("\n\n")) return false;
      }
      if (!silent) {
        const t = state.push("math", "", 0);
        t.content = body;
        t.meta = {display};
      }
      state.pos = end + close.length;
      return true;
    });
    md.block.ruler.before("paragraph", "math_block", (state, startLine, endLine, silent) => {
      const lineText = l => state.src.slice(state.bMarks[l] + state.tShift[l], state.eMarks[l]);
      const first = lineText(startLine).trim();
      const pair = first.startsWith("$$") ? ["$$", "$$"] : first.startsWith("\\[") ? ["\\[", "\\]"] : null;
      if (!pair || state.sCount[startLine] - state.blkIndent >= 4) return false;
      let body = first.slice(2), line = startLine, found = false;
      if (body.trim().endsWith(pair[1]) && body.trim().length >= 2) {
        body = body.trim().slice(0, -2);
        found = true;
      } else {
        const parts = [body];
        while (++line < endLine) {
          const t = lineText(line);
          if (t.trim().endsWith(pair[1])) { parts.push(t.trim().slice(0, -2)); found = true; break; }
          parts.push(t);
        }
        body = parts.join("\n");
      }
      if (!found || !body.trim()) return false;
      if (silent) return true;
      const t = state.push("math_block", "", 0);
      t.content = body;
      t.map = [startLine, line + 1];
      state.line = line + 1;
      return true;
    }, {alt: ["paragraph", "reference", "blockquote", "list"]});
    md.renderer.rules.math = (tokens, i) => tex(tokens[i].content, tokens[i].meta.display);
    md.renderer.rules.math_block = (tokens, i) => `<div class="math-block">${tex(tokens[i].content, true)}</div>`;
  }

  /* ---------- markdown-it instance ---------- */
  let md = null;
  function engine() {
    if (md || !ok()) return md;
    md = window.markdownit({html: false, linkify: true, breaks: false, typographer: false,
      highlight(code, lang) {
        if (window.hljs && lang && hljs.getLanguage(lang)) {
          try { return hljs.highlight(code, {language: lang, ignoreIllegals: true}).value; } catch (e) { /* plain */ }
        }
        return "";
      }});
    if (window.markdownitFootnote) md.use(window.markdownitFootnote);
    if (window.markdownitTaskLists) md.use(window.markdownitTaskLists, {enabled: false, label: true});
    mathPlugin(md);
    const fence = md.renderer.rules.fence;
    md.renderer.rules.fence = (tokens, i, opts, env, self) => {
      const t = tokens[i], info = (t.info || "").trim(), lang = info.split(/\s+/)[0].toLowerCase();
      if (lang === "mermaid") return mermaidBlock(t.content);
      const label = info ? `<span class="cb-lang">${esc(info)}</span>` : "";
      return `<div class="codeblk"><div class="cb-bar">${label}<button type="button" class="cb-copy">copy</button></div>${fence(tokens, i, opts, env, self)}</div>`;
    };
    const link = md.renderer.rules.link_open || ((tokens, i, opts, env, self) => self.renderToken(tokens, i, opts));
    md.renderer.rules.link_open = (tokens, i, opts, env, self) => {
      tokens[i].attrSet("target", "_blank");
      tokens[i].attrSet("rel", "noopener noreferrer");
      return link(tokens, i, opts, env, self);
    };
    return md;
  }
  // the source lives in the hidden <pre> (DOMPurify drops attributes containing "-->", common in flowcharts)
  const mermaidBlock = src => `<div class="mmd"><div class="mmd-bar"><span class="cb-lang">mermaid</span>` +
    `<button type="button" data-mmd="src">source</button><button type="button" data-mmd="svg">SVG</button>` +
    `<button type="button" data-mmd="png">PNG</button></div><div class="mmd-out"><span class="tid">diagram…</span></div>` +
    `<pre class="mmd-src" hidden><code>${esc(src)}</code></pre></div>`;

  const PURIFY = {ADD_ATTR: ["target"], FORBID_TAGS: ["style", "form", "iframe", "object", "embed"],
                  FORBID_ATTR: ["onerror", "onload"]};
  if (window.DOMPurify) {
    DOMPurify.addHook("afterSanitizeAttributes", node => {  // only safe link targets survive
      if (node.tagName === "A" && node.getAttribute("target")) node.setAttribute("rel", "noopener noreferrer");
    });
  }

  function render(text) {
    const m = engine();
    if (!m) return `<div class="md-plain">${esc(text)}</div>`;
    try {
      return DOMPurify.sanitize(m.render(String(text ?? "")), PURIFY);
    } catch (e) {
      return `<div class="md-plain">${esc(text)}</div>`;
    }
  }

  /* ---------- mermaid (loaded on first use, rendered when visible) ---------- */
  let mermaidReady = null, seq = 0;
  function loadMermaid() {
    if (!mermaidReady) mermaidReady = new Promise((res, rej) => {
      const s = document.createElement("script");
      s.src = "/static/vendor/mermaid.min.js";
      s.onload = () => {
        const dark = !document.documentElement.dataset.theme || document.documentElement.dataset.theme === "dark";
        mermaid.initialize({startOnLoad: false, securityLevel: "strict", theme: dark ? "dark" : "default",
                            htmlLabels: false, flowchart: {htmlLabels: false}, fontFamily: "system-ui, sans-serif"});
        res(window.mermaid);
      };
      s.onerror = () => rej(new Error("mermaid failed to load"));
      document.head.appendChild(s);
    });
    return mermaidReady;
  }
  async function drawMermaid(box) {
    if (box.dataset.done) return;
    box.dataset.done = "1";
    const out = box.querySelector(".mmd-out"), src = box.querySelector(".mmd-src").textContent;
    try {
      const m = await loadMermaid();
      const {svg} = await m.render(`mmd-${Date.now().toString(36)}-${++seq}`, src);
      out.innerHTML = DOMPurify.sanitize(svg, {USE_PROFILES: {svg: true, svgFilters: true}, ADD_TAGS: ["style"]});
    } catch (e) {
      out.innerHTML = `<div class="math-err">Diagram error: ${esc(String(e.message || e).slice(0, 300))}</div>`;
      box.querySelector(".mmd-src").hidden = false;
    }
  }
  const io = "IntersectionObserver" in window ? new IntersectionObserver(entries => {
    for (const e of entries) if (e.isIntersecting) { io.unobserve(e.target); drawMermaid(e.target); }
  }, {rootMargin: "200px"}) : null;

  function download(name, blob) {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  }
  function exportDiagram(box, kind) {
    const svg = box.querySelector(".mmd-out svg");
    if (!svg) return;
    const xml = new XMLSerializer().serializeToString(svg);
    if (kind === "svg") return download("diagram.svg", new Blob([xml], {type: "image/svg+xml"}));
    const r = svg.getBoundingClientRect(), scale = 2, img = new Image();
    img.onload = () => {
      const c = document.createElement("canvas");
      c.width = Math.max(1, r.width * scale); c.height = Math.max(1, r.height * scale);
      const g = c.getContext("2d");
      g.fillStyle = getComputedStyle(document.body).backgroundColor || "#0f1115";
      g.fillRect(0, 0, c.width, c.height);
      g.drawImage(img, 0, 0, c.width, c.height);
      c.toBlob(b => b && download("diagram.png", b), "image/png");
    };
    img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(xml);
  }

  function enhance(root) {
    for (const b of root.querySelectorAll(".cb-copy")) {
      if (b.dataset.wired) continue;
      b.dataset.wired = "1";
      b.addEventListener("click", async ev => {
        ev.stopPropagation();
        const code = b.closest(".codeblk").querySelector("pre code, pre");
        try { await navigator.clipboard.writeText(code.textContent); b.textContent = "copied"; }
        catch (e) { b.textContent = "copy failed"; }
        setTimeout(() => (b.textContent = "copy"), 1200);
      });
    }
    for (const box of root.querySelectorAll(".mmd")) {
      if (box.dataset.wired) continue;
      box.dataset.wired = "1";
      box.querySelectorAll("[data-mmd]").forEach(b => b.addEventListener("click", ev => {
        ev.stopPropagation();
        if (b.dataset.mmd === "src") { const p = box.querySelector(".mmd-src"); p.hidden = !p.hidden; }
        else exportDiagram(box, b.dataset.mmd);
      }));
      if (io) io.observe(box); else drawMermaid(box);
    }
    root.querySelectorAll("a[href]").forEach(a => a.addEventListener("click", ev => ev.stopPropagation()));
  }

  function slug(t, used) {
    let s = t.toLowerCase().replace(/[^\w\- ]+/g, "").trim().replace(/\s+/g, "-") || "section", k = s, n = 1;
    while (used.has(k)) k = `${s}-${++n}`;
    used.add(k);
    return k;
  }
  function toc(root, min = 3) {
    const hs = [...root.querySelectorAll("h1, h2, h3")], used = new Set();
    if (hs.length < min) return "";
    const items = hs.map(h => {
      h.id = "md-" + slug(h.textContent, used);
      return `<a href="#${h.id}" class="toc-${h.tagName.toLowerCase()}" data-toc="${h.id}">${esc(h.textContent)}</a>`;
    });
    return `<nav class="toc"><div class="tid">Contents</div>${items.join("")}</nav>`;
  }

  /** True when text looks like it has Markdown worth rendering (cheap check for short messages). */
  const looksMarkdown = t => /[`*_#>\[\]|$\\~-]|^\s*\d+\.\s|https?:\/\//m.test(t || "");

  window.MD = {render, enhance, toc, looksMarkdown, ready: ok, tex};
})();
