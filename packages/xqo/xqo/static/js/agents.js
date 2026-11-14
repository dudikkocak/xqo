/* Departments & agents panel */
/* ---------------- agents ---------------- */
const dcolor=d=>d==="hq"?"#7aa2ff":`hsl(${hue(d)},65%,64%)`;
function agentRow(a){
  const al=a.alerts&&a.alerts.count-(a.attention||0)>0;
  const st=a.attention?`<span class="load att">⚠ waiting for you</span>`:al?`<span class="load att" title="${esc(a.alerts.kinds.join(", "))}: see Needs your attention">⚠ ${a.alerts.count-(a.attention||0)} alert${a.alerts.count-(a.attention||0)>1?"s":""}</span>`:`<span class="load ${a.active?'busy':''}">${a.active?a.active+' active':'idle'}</span>`;
  return `<div class="agent inn">${st}${avChip(a.avatar)}<b${nickTitle(a.name)}>${esc(nick(a.name))}</b>${a.head?'<span class="chip">head</span>':''}${a.can_run_code?'<span class="chip">runs code</span>':''}${(a.mcp||[]).map(m=>`<span class="chip">mcp:${esc(m)}</span>`).join("")}<div class="sk">${esc(a.skills.join(", "))}</div><div class="sk">${modelLine(a)} · ${a.usage.calls} calls · ${fmt(a.usage.input_tokens)} in / ${fmt(a.usage.output_tokens)} out${a.usage.cache_read_input_tokens?` · ${fmt(a.usage.cache_read_input_tokens)} cached`:""}${a.compacted&&a.compacted.chars?` · <span title="old tool output trimmed from the history (${a.compacted.blocks} blocks)">~${fmt(Math.round(a.compacted.chars/4))} tok trimmed</span>`:""}${a.cutoffs&&a.cutoffs.cut?` · <span class="cutw" title="replies that hit max_tokens (${fmt(a.max_tokens)}): ${a.cutoffs.continued} continued, ${a.cutoffs.gave_up} left unfinished">${a.cutoffs.cut} cut off</span>`:""}</div></div>`}
function deptHeader(d){
  const u=d.usage||{},b=d.budget||{},root=$("filter").value||state.lastRoot,cur=(u.by_task||{})[root];
  const caps=[b.max_tokens?`${fmt(b.max_tokens)} tok`:"",b.max_cost_usd?`$${b.max_cost_usd}`:""].filter(Boolean).join(" / ");
  const pct=cur&&(b.max_tokens||b.max_cost_usd)?Math.min(100,100*Math.max(b.max_tokens?cur.tokens/b.max_tokens:0,b.max_cost_usd?cur.cost/b.max_cost_usd:0)):null;
  const contacts=d.contacts==="*"?"all departments":(d.contacts||[]).join(", ")||"own only";
  return `<div class="dept" style="--dc:${dcolor(d.name)}" title="double-click to collapse / expand its agents"><div class="dh"><span class="tw">${state.openDepts.has(d.name)?"▾":"▸"}</span><b>${esc(d.name)}</b><span class="nag">${d.agents.length} agent${d.agents.length===1?"":"s"}</span><span class="spacer"></span><span class="load ${d.active?'busy':''}">${d.active?d.active+' active':'idle'}</span></div>
    ${d.head?`<div class="sk hd">head <span class="tid">${esc(d.head)}</span></div>`:""}
    ${d.description?`<div class="sk">${esc(d.description)}</div>`:""}
    <div class="sk">${fmt(u.tokens||0)} tokens · $${(u.cost||0).toFixed(3)} total${caps?` · budget ${caps} per task`:""}${d.name!=="hq"?` · reaches ${esc(contacts)}`:""}</div>
    ${pct!==null?`<div class="bar" title="${esc(tname(root))}: ${fmt(cur.tokens)} tokens, $${cur.cost.toFixed(3)}"><i style="width:${pct}%"></i></div>`:""}</div>`}
const LAYOUTS={departments:"By department",ring:"Ring",grid:"Grid",star:"Star"};
function graphLayout(){const v=store.get("graphLayout",null)||state.layoutDefault;return LAYOUTS[v]?v:Object.keys(LAYOUTS)[0]}
function initLayoutPicker(){const sel=$("glayout");sel.innerHTML=Object.entries(LAYOUTS).map(([k,v])=>`<option value="${k}">${v}</option>`).join("");
  sel.value=graphLayout();sel.onchange=()=>{store.set("graphLayout",sel.value);buildGraph();updateGraph()}}
function arrange(names,mode,W,H,ex){
  /* positions for the user (first) and the agents in a W×H box; ex = extra room for labels under avatars */
  const pos={},ag=names.slice(1);
  if(mode==="grid"){const cols=Math.max(1,Math.ceil(Math.sqrt(ag.length*W/(H-70)))),rows=Math.max(1,Math.ceil(ag.length/cols)),
      cw=W/cols,rh=Math.max(78+ex,(H-70)/rows);pos[names[0]]=[W/2,34];
    ag.forEach((a,i)=>{const r=Math.floor(i/cols),inRow=Math.min(cols,ag.length-r*cols),off=(W-inRow*cw)/2;pos[a]=[off+cw*(i%cols+.5),70+rh*(r+.5)]});
    return {pos,vb:[0,0,W,Math.max(H,70+rh*rows)+ex]}}
  const ring=mode==="star"?ag:names;
  if(mode==="star")pos[names[0]]=[W/2,H/2];
  ring.forEach((a,i)=>{const t=-Math.PI/2+i*2*Math.PI/ring.length;pos[a]=[W/2+W*.37*Math.cos(t),H/2+H*.37*Math.sin(t)]});
  return {pos,vb:[0,0,W,H+ex]};
}
/* display name: the agent's / user's "nickname" when set; ids (events, tools, API values) always stay the real name */
function nick(n){if(n==="user")return state.userNickname||"user";const a=(state.agents||[]).find(a=>a.name===n);return a&&a.nickname||n}
function nickTitle(n){return nick(n)!==n?` title="${esc(n)}"`:""}
function avatarOf(n){return n==="user"?state.userAvatar:(state.agents.find(a=>a.name===n)||{}).avatar}
function avDef(n){const av=avatarOf(n);return av&&av.kind==="image"?`<pattern id="av_${esc(n)}" patternContentUnits="objectBoundingBox" width="1" height="1"><image href="${esc(av.url)}" width="1" height="1" preserveAspectRatio="xMidYMid slice"/></pattern>`:""}
function avChip(av){return !av?"":av.kind==="image"?`<img class="avi" src="${esc(av.url)}" alt="">`:`<span class="avi txt">${esc(av.text)}</span>`}
async function loadAgents(){
  if(state.userAvatar===undefined)state.userAvatar=await fetch("/api/config").then(r=>r.json()).then(c=>{state.layoutDefault=c.graph_layout;if(c.limits)Object.assign(ALIMIT,{image:c.limits.image_mb*1e6,pdf:c.limits.pdf_mb*1e6,text:c.limits.text_attachment_mb*1e6});state.userNickname=c.user_nickname||null;return c.user_avatar||null}).catch(()=>null);
  if(!$("glayout").options.length)initLayoutPicker();
  const [ag,dp]=await Promise.all([fetch("/api/agents").then(r=>r.json()),fetch("/api/departments").then(r=>r.json())]);
  state.agents=ag;state.depts=dp;state.deptOf={};
  for(const d of dp)for(const a of d.agents)state.deptOf[a.name]=d.name;
  const multi=dp.length>1;
  $("agents").innerHTML=multi?dp.map(d=>`<div class="dgrp${state.openDepts.has(d.name)?"":" shut"}" data-dept="${esc(d.name)}">${deptHeader(d)}${d.agents.map(agentRow).join("")}</div>`).join(""):ag.map(agentRow).join("");
  const sig=dp.map(d=>d.name+":"+d.agents.map(a=>a.name).join(",")).join("|");
  if(state.sig!==sig&&$("to").options.length)fillTo();
  if(!$("entry").options.length||state.sig!==sig){const cur=$("entry").value;
    $("entry").innerHTML=dp.map(d=>`<optgroup label="${esc(d.name)}">${d.agents.map(a=>`<option value="${esc(a.name)}" ${a.name===cur?"selected":""}>${esc(nick(a.name))}</option>`).join("")}</optgroup>`).join("")}
  syncDeptSelects(dp,multi);
  if(!$("graph").querySelector(".gnode")||state.sig!==sig){state.sig=sig;buildGraph()}
  updateGraph();
}
$("agents").addEventListener("dblclick",ev=>{
  if(ev.target.closest("a,button,input,select,textarea,summary"))return;
  const h=ev.target.closest(".dept"),g=h&&h.parentElement;if(!g||!g.classList.contains("dgrp"))return;
  const shut=g.classList.toggle("shut"),n=g.dataset.dept;shut?state.openDepts.delete(n):state.openDepts.add(n);
  h.querySelector(".tw").textContent=shut?"▸":"▾";
  try{window.getSelection().removeAllRanges()}catch{}  // a double-click also selects a word
});
function syncDeptSelects(dp,multi){
  $("deptf").style.display=multi?"":"none";
  const names=dp.map(d=>d.name),fv=$("deptf").value,nv=$("nscope").value;
  $("deptf").innerHTML=`<option value="">all departments</option>`+names.map(n=>`<option ${n===fv?"selected":""}>${esc(n)}</option>`).join("");
  $("nscope").style.display=multi?"":"none";
  $("nscope").innerHTML=`<option value="">task</option>`+names.filter(n=>n!=="hq").map(n=>`<option ${n===nv?"selected":""}>${esc(n)}</option>`).join("");
}
$("dreload").onclick=async()=>{const r=await fetch("/api/departments/reload",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});
  const d=await r.json().catch(()=>({}));if(!r.ok){alert(d.detail||r.statusText);return}
  $("status").textContent=`reloaded: ${d.departments.join(", ")||"no departments"}`;state.sig=null;await loadAgents()};
const loadAgentsSoon=debounce(loadAgents,150);
const SRC={agent:"set on the agent",department:"department default",config:"default_model in agents.json",default:"built-in default (XQO_MODEL)"};
const PSRC={agent:"set on the agent",department:"department default_provider",config:"default_provider in agents.json",default:"built-in default",fallback:"Claude Agent SDK unavailable: fell back to the Anthropic API"};
function providerChip(a){const p=a.provider||"anthropic",i=a.provider_info||{},src=a.provider_source||"default";
  const key=i.auth?`sign-in: ${i.auth}`+(a.sdk_tools?` · Claude Code tools: ${a.sdk_tools.join(", ")||"none"}`:""):
    i.api_key_env?(i.key_set?`${i.api_key_env} is set`:`${i.api_key_env} is NOT set`):"no key needed";
  const label=p.length>28?p.replace(/^https?:\/\//,"").slice(0,26)+"…":p;
  return `<span class="chip prov${(i.auth?i.auth==="none found":i.api_key_env&&!i.key_set&&p!=="anthropic")||src==="fallback"?" warn":""}" title="Provider ${esc(p)} (${esc(PSRC[src]||src)})${i.base_url?` · ${esc(i.base_url)}`:""} · ${esc(key)}">${esc(label)}</span>`}
function modelLine(a){const src=a.model_source||"agent",served=a.served_model&&a.served_model!==a.model?a.served_model:"";
  return `${providerChip(a)}<span class="mdl" title="Model: ${esc(SRC[src]||src)}${a.effort?` · effort ${esc(a.effort)}`:""}${a.max_tokens?` · max_tokens ${a.max_tokens} (${esc(a.max_tokens_source||"default")})`:""}">${esc(a.model)}</span>${src!=="agent"?`<span class="chip" title="${esc(SRC[src]||src)}">${src==="department"?"dept default":"default"}</span>`:""}${a.effort?`<span class="chip">${esc(a.effort)}</span>`:""}${served?` <span class="tid" title="the last call was served by a fallback model">→ ${esc(served)}</span>`:""}`}

