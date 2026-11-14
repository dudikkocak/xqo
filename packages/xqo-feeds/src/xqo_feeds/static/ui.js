/* feeds plugin UI: the 📡 feeds button (with its alert badge) and dialog */
XQO.button({id:"tfeeds",label:'📡 feeds<span id="feedbadge"></span>',title:"Local feeds: what agents watch on this machine, rules and alerts",place:"tasks",hidden:true,onclick:()=>openFeeds()});
/* ---------------- feeds: what agents watch on this machine (feeds/), rules and alerts ---------------- */
const FD={open:new Set()};
async function loadFeedBadge(){let r;try{r=await api("/api/feeds")}catch(err){return}
  $("tfeeds").style.display=r.enabled?"":"none";if(!r.enabled)return r;
  const n=(r.alerts||[]).length;$("feedbadge").style.display=n?"inline":"none";$("feedbadge").textContent=n?` ⚠${n}`:"";return r}
function spark(points){if(points.length<2)return "";const lo=Math.min(...points),hi=Math.max(...points),span=hi-lo||1;
  return `<svg viewBox="0 0 70 18" preserveAspectRatio="none"><polyline points="${points.map((v,i)=>`${(i/(points.length-1)*70).toFixed(1)},${(17-(v-lo)/span*16).toFixed(1)}`).join(" ")}"/></svg>`}
const SPARK=["cpu.percent","mem.percent","disk.percent","load.1","running","mem.mb","restarts"];
function feedVals(f){const out=[];for(const [key,s] of Object.entries(f.latest||{})){const hist=(f.history||{})[key]||[];
    const rank=k=>SPARK.includes(k)?SPARK.indexOf(k):99;
    const vals=Object.entries(s.fields).filter(([k,v])=>v!==null&&v!==""&&!(Array.isArray(v)&&!v.length)).sort((a,b)=>rank(a[0])-rank(b[0])).slice(0,12).map(([k,v])=>{
      const pts=SPARK.includes(k)?hist.map(h=>h[1][k]).filter(x=>typeof x==="number"):[];
      return `<span title="${esc(k)}">${key?`<b>${esc(key)}</b> `:""}${esc(k)}=<b>${esc(Array.isArray(v)?v.join(","):String(v))}</b>${spark(pts)}</span>`});
    out.push(...vals)}return out.join("")}
function sev(s){return s&&s!=="info"?`<span class="sev ${esc(s)}">${esc(s)}</span>`:""}
function fwhen(t){return t?rel(new Date(t*1000).toISOString()):"–"}
function openFeeds(){dialog("Local feeds",'<div id="fbody"></div>',"feeds");drawFeeds()}
async function drawFeeds(){
  if($("dlg").dataset.kind!=="feeds"||$("dlg").style.display!=="flex")return;
  const box=$("fbody");const r=await loadFeedBadge();if(!r)return;
  if(!r.enabled){box.innerHTML='<div class="sub">No feeds configured. Add "feeds" and "feed_rules" to agents.json (see FEEDS.md).</div>';return}
  const keep=[...box.querySelectorAll("details[open]")].map(d=>d.dataset.k);keep.forEach(k=>FD.open.add(k));
  const alerts=r.alerts||[],rules=r.rules||[],feeds=Object.values(r.feeds||{});
  box.innerHTML=(r.running?"":`<div class="sub" style="color:var(--attention)">Feeds are not watched in this process${(r.warnings||[]).length?": "+esc(r.warnings.join("; ")):""}.</div>`)+
    `<div class="fsec">Alerts (${alerts.length} open)<span class="spacer"></span>${alerts.length?'<button class="ghost" id="fackall">Acknowledge all</button>':""}</div>`+
    (alerts.slice().reverse().map(a=>`<div class="frow al" data-id="${esc(a.id)}"><span class="nm" title="${esc(a.text)}">⚠ ${esc(a.rule)}${a.count>1?` ×${a.count}`:""} ${sev(a.severity)} ${a.test?'<span class="sev">test</span>':""}</span>
      <span class="acts"><button class="ghost" data-ack>Acknowledge</button></span><span class="meta"><span>${esc(a.text)}</span><span>first ${fwhen(a.first)}</span><span>last ${fwhen(a.last)}</span></span></div>`).join("")||'<div class="sub">No open alerts.</div>')+
    `<div class="fsec">Rules (${rules.length})</div>`+
    (rules.map(x=>{const c=x.counts||{},muted=x.muted_until&&x.muted_until*1000>Date.now();
      return `<div class="frow" data-rule="${esc(x.name)}"><span class="nm" title="${esc(x.description||"")}">${x.firing.length?"🔴 ":""}${esc(x.name)} <span class="tid">${esc(x.action)}${["wake","digest"].includes(x.action)?` · ${esc(x.mode)} · ${esc(x.agent)}`:""}</span></span>
      <span class="acts">${muted?'<button class="ghost" data-unmute>Unmute</button>':'<button class="ghost" data-mute title="Mute this rule for an hour">Mute 1h</button>'}<button class="ghost" data-test title="Run this rule's action now with a test event">Test</button>${x.schedule?'<button class="ghost" data-runs>Runs</button>':""}</span>
      <span class="meta"><span>feed: ${esc(x.feed)}</span>${x.kind?`<span>kind: ${esc([].concat(x.kind).join(", "))}</span>`:""}${x.severity?`<span>≥ ${esc(x.severity)}</span>`:""}${x.when?`<span>when <code>${esc(x.when)}</code>${x.for?` for ${esc(String(x.for))}s`:""}</span>`:""}${x.clear?`<span>clear <code>${esc(x.clear)}</code></span>`:""}
        ${x.action==="digest"?`<span>every ${Math.round(x.every/60)}m, ≥ ${x.min_events} event(s)${x.pending_digest?`, ${x.pending_digest} waiting`:""}</span>`:""}
        <span>fired ${c.fired||0}</span>${c.woken?`<span>tasks ${c.woken}</span>`:""}${c.followups?`<span>follow-ups ${c.followups}</span>`:""}${c.deduped?`<span>deduped ${c.deduped}</span>`:""}${c.suppressed?`<span style="color:var(--attention)">capped ${c.suppressed}</span>`:""}${c.quiet?`<span>quiet ${c.quiet}</span>`:""}
        ${x.cost_usd?`<span>$${x.cost_usd.toFixed(2)}</span>`:""}<span>last ${fwhen(x.last_fired)}</span>${muted?`<span style="color:var(--attention)">muted until ${esc(new Date(x.muted_until*1000).toLocaleTimeString())}</span>`:""}${x.quiet_now?"<span>quiet hours now</span>":""}
        ${x.mode==="act"&&["wake","digest"].includes(x.action)?`<span>act: ${esc([...x.tools,...x.actions.map(a=>"action "+a)].join(", ")||"–")}${x.approve?" (approved one by one)":""}</span>`:""}</span>
      ${x.firing.length?`<span class="warnline">firing: ${esc(x.firing.map(k=>k||"(all)").join(", "))}</span>`:""}</div>`}).join("")||'<div class="sub">No rules: events are only logged.</div>')+
    `<div class="fsec">Feeds (${feeds.length})</div>`+
    feeds.map(f=>`<div class="frow"><span class="nm">${esc(f.name)} <span class="tid">${esc(f.type)} · ${esc(f.about)}${f.trust==="untrusted"?" · untrusted text":""}</span></span>
      <span class="acts"><span class="tid">${f.events||0} event(s)${f.dropped?`, ${f.dropped} not logged`:""} · ${fwhen(f.last)}</span></span>
      ${f.error?`<span class="warnline">${esc(f.error)}</span>`:""}<span class="vals">${feedVals(f)}</span>
      ${(f.recent||[]).length?`<details data-k="${esc(f.name)}" ${FD.open.has(f.name)?"open":""}><summary>recent events</summary><pre>${esc(f.recent.slice().reverse().map(e=>`${new Date(e.ts*1000).toLocaleTimeString()} ${e.kind} [${e.severity}] ${e.key&&!e.text?e.key+" ":""}${e.text||Object.entries(e.fields).map(([k,v])=>k+"="+v).join(", ")}`).join("\n"))}</pre></details>`:""}</div>`).join("");
  box.querySelectorAll("details").forEach(d=>d.ontoggle=()=>d.open?FD.open.add(d.dataset.k):FD.open.delete(d.dataset.k));
  const call=async(url,body)=>{try{await api(url,{method:"POST",body:JSON.stringify(body||{})})}catch(err){alert(err.message)}drawFeeds()};
  if($("fackall"))$("fackall").onclick=()=>call("/api/feeds/alerts/all/ack");
  box.querySelectorAll("[data-ack]").forEach(b=>b.onclick=()=>call(`/api/feeds/alerts/${encodeURIComponent(b.closest(".frow").dataset.id)}/ack`));
  const rn=b=>encodeURIComponent(b.closest(".frow").dataset.rule);
  box.querySelectorAll("[data-mute]").forEach(b=>b.onclick=()=>call(`/api/feeds/rules/${rn(b)}/mute`,{minutes:60}));
  box.querySelectorAll("[data-unmute]").forEach(b=>b.onclick=()=>call(`/api/feeds/rules/${rn(b)}/unmute`));
  box.querySelectorAll("[data-test]").forEach(b=>b.onclick=()=>{const x=rules.find(y=>y.name===b.closest(".frow").dataset.rule);
    if(["wake","digest"].includes(x.action)&&!confirm(`Test ${x.name}: this ${x.action==="wake"?"starts a task for "+x.agent:"adds a test event to the next digest"}. Go on?`))return;call(`/api/feeds/rules/${rn(b)}/test`,{text:"test event from the dashboard"})});
  box.querySelectorAll("[data-runs]").forEach(b=>b.onclick=async()=>{const sid=rules.find(y=>y.name===b.closest(".frow").dataset.rule).schedule;
    let s;try{s=await api(`/api/schedules/${encodeURIComponent(sid)}`)}catch(err){return alert(err.message)}openSchedules("runs",s)});
}
const drawFeedsSoon=debounce(drawFeeds,500);
loadFeedBadge();
setInterval(()=>{if($("dlg").style.display==="flex"&&$("dlg").dataset.kind==="feeds")drawFeeds()},10000);
XQO.on(e=>{
    if(e.kind.startsWith("feed")){if(e.kind==="feed_alert"){loadFeedBadge();if(!(e.meta||{}).test&&(e.meta||{}).count===1||!(e.meta||{}).count)toast(`📡 ${e.content}`,null,{label:"Feeds",run:()=>openFeeds()})}
      if($("dlg").style.display==="flex"&&$("dlg").dataset.kind==="feeds")drawFeedsSoon()}
});
