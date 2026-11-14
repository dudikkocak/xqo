/* Message flow, task usage, follow-scroll, the header usage chip, and onEvent (every event goes through it) */
/* ---------------- timeline ---------------- */
const MDKINDS=new Set(["task","direct","reply","broadcast","handover","result","done","error","fork","attention_requested","user_message","agent_reply"]);
const mdCache=new Map();
function mdHtml(e){let h=mdCache.get(e.id);if(h===undefined){h=MD.render(e.content);if(mdCache.size>3000)mdCache.clear();mdCache.set(e.id,h)}return h}
function render(e){
  const f=$("filter").value;
  if(f&&e.root_task_id!==f)return null;
  if($("hidellm").checked&&e.kind==="llm")return null;
  const df=$("deptf").value;if(df&&e.dept!==df&&e.dept_to!==df)return null;
  const rich=MDKINDS.has(e.kind)&&!!e.content&&window.MD&&MD.ready()&&MD.looksMarkdown(e.content);
  const d=document.createElement("div");d.className="ev"+(e.kind==="user_message"?" chat me":e.kind==="agent_reply"?" chat bot":"");d.dataset.root=e.root_task_id||"";d.style.setProperty("--k",`var(--${e.kind})`);
  const to=e.recipient?` → <span class="who">${esc(e.recipient==="*"?"ALL":nick(e.recipient))}</span>`:"";
  const mm=e.meta||{},lbl=e.kind==="user_message"?(mm.answered_attention?"answer":mm.kind||"message"):e.kind==="agent_reply"?"reply":e.kind.replace("_"," ");
  d.innerHTML=`<div class="h"><span class="kind">${esc(lbl)}</span>${mm.urgent&&(e.kind==="user_message"||e.kind==="agent_reply")?'<span class="ub">urgent</span>':""}<span class="who">${esc(nick(e.sender))}</span>${e.dept&&state.depts&&state.depts.length>1?`<span class="dtag" style="--dc:${dcolor(e.dept)}">${esc(e.dept)}</span>`:""}${to}<span class="tid">${esc(e.task_name||e.task_id||"")}</span><span class="ts">${esc(e.ts.slice(11,23))}</span>${rich?'<button class="ghost rawb" title="Show the raw text">raw</button>':""}${e.root_task_id?'<button class="ghost fk" title="Branch a new task from this point">⑂ fork</button>':""}</div><div class="body${rich?" md":""}">${rich?mdHtml(e):esc(e.content)}</div>${attLinks(e.root_task_id,(e.meta||{}).attachments)}`;
  d.title="double-click to expand / collapse";
  d.ondblclick=ev=>{if(ev.target.closest("a,button,input,select,textarea,summary"))return;
    d.classList.toggle("open");try{window.getSelection().removeAllRanges()}catch{}};  // a double-click also selects a word
  const fk=d.querySelector(".fk");if(fk)fk.onclick=ev=>{ev.stopPropagation();openFork(e)};
  const rb=d.querySelector(".rawb");if(rb)rb.onclick=ev=>{ev.stopPropagation();const b=d.querySelector(".body"),raw=!rb.classList.contains("on");
    rb.classList.toggle("on",raw);b.classList.toggle("md",!raw);b.classList.toggle("raw",raw);b.innerHTML=raw?esc(e.content):mdHtml(e);if(!raw)MD.enhance(b)};
  if(rich)MD.enhance(d);return d;
}
function rerender(){if(R.on)return drawReplay();const box=$("events");box.innerHTML="";for(const e of state.events){const d=render(e);if(d)box.append(d)}FL.stick()}
function addTask(id,name){if(id&&!state.tasks.has(id)){state.tasks.add(id);$("filter").insertAdjacentHTML("beforeend",`<option value="${esc(id)}">${esc(name||id)}</option>`)}}
const testBadge=t=>t.tests&&t.tests.runs?(t.tests.last_exit===0?`<span class="tbadge pass" title="${t.tests.runs} run(s)">tests pass</span>`:`<span class="tbadge fail" title="${t.tests.failed} of ${t.tests.runs} runs failed">tests failing</span>`):"";
/* Claude Code usage per task (from its sessions' result messages); a parent also shows its whole sub-tree */
const UK=["input_tokens","output_tokens","cache_read_input_tokens","cache_creation_input_tokens"];
const usageSum=t=>{const s={cost_usd:+(t.usage&&t.usage.cost_usd)||0,turns:+(t.usage&&t.usage.turns)||0};for(const k of UK)s[k]=+(t.usage&&t.usage[k])||0;
  for(const c of t.children||[]){const x=usageSum(c);for(const k in s)s[k]+=x[k]}return s};
const usd=n=>"$"+(n<0.01&&n>0?n.toFixed(4):n.toFixed(2));
const toks=u=>UK.reduce((a,k)=>a+(+u[k]||0),0);
function usageBadge(t){const u=t.usage||{},tree=(t.children||[]).length?usageSum(t):null;if(!u.sessions&&!(tree&&toks(tree)))return "";
  const tip=x=>`${fmt(x.input_tokens||0)} in · ${fmt(x.output_tokens||0)} out · ${fmt(x.cache_read_input_tokens||0)} cache read · ${fmt(x.cache_creation_input_tokens||0)} cache write · ${x.turns||0} turns`;
  const own=u.sessions?`${usd(+u.cost_usd||0)} · ${fmt(toks(u))} tok`:"";
  const sub=tree&&toks(tree)!==toks(u)?`${own?" · ":""}tree ${usd(tree.cost_usd)} · ${fmt(toks(tree))} tok`:"";
  return `<span class="tbadge usage" title="${esc((u.sessions?`This task: ${tip(u)}${u.duration_ms?` · ${Math.round(u.duration_ms/1000)}s`:""}`:"")+(sub?`${u.sessions?"\n":""}With sub-tasks: ${tip(tree)}`:""))}">${own}${sub}</span>`}
function node(t){return `<div class="tnode${state.openNodes.has(t.id)?" open":""}" data-id="${esc(t.id)}" title="double-click to expand / collapse"><span class="st ${esc(t.status)}">${esc(t.status.replace("_"," "))}</span>${t.schedule?`<span class="tbadge sch" title="Started by the schedule ${esc(t.schedule.name)} (${esc(t.schedule.reason||"schedule")})">⏱ ${esc(t.schedule.name)} #${esc(t.schedule.run)}</span>`:""}${t.archived?'<span class="tbadge" style="color:var(--mute)">archived</span>':""}${testBadge(t)}${usageBadge(t)} <b>${esc(nick(t.assignee))}</b> <span class="tid">${esc(t.name||t.id)} · from ${esc(t.requester)}</span>
  ${t.forked_from?`<div class="fork">⑂ forked from ${esc(t.forked_from.task_name||t.forked_from.task_id)} at its ${esc(t.forked_from.event_kind)} event (${esc((t.forked_from.event_ts||"").slice(11,19))})</div>`:""}
  ${attLinks(t.root_id,t.attachments)}
  ${window.MD&&MD.ready()?`<div class="md tres">${MD.render((t.result??t.prompt??"").slice(0,20000))}</div>`:`<pre>${esc((t.result??t.prompt??"").slice(0,4000))}</pre>`}${t.children.map(node).join("")}</div>`}
$("taskinfo").addEventListener("dblclick",ev=>{
  if(ev.target.closest("a,button,input,select,textarea,summary"))return;
  const n=ev.target.closest(".tnode");if(!n)return;
  const open=n.classList.toggle("open");open?state.openNodes.add(n.dataset.id):state.openNodes.delete(n.dataset.id);
  try{window.getSelection().removeAllRanges()}catch{}  // a double-click also selects a word
});
/* Live viewer and message flow follow new output (newest at the bottom) until the user scrolls up;
   staying at the bottom for FOLLOW_MS without scrolling up again resumes following */
const atBottom=el=>el.scrollHeight-el.scrollTop-el.clientHeight<8;
const FOLLOW_MS=1500;
function follower(el){
  const f={on:true,last:0,t:0,raf:0,
    stick(){if(f.on){el.scrollTop=el.scrollHeight;f.last=el.scrollTop}},
    soon(){if(!f.raf)f.raf=requestAnimationFrame(()=>{f.raf=0;f.stick()})},  // one layout a frame while events stream in
    resume(){clearTimeout(f.t);f.t=0;f.on=true;f.stick()},
    arm(){if(atBottom(el)){if(!f.t)f.t=setTimeout(f.resume,FOLLOW_MS)}else{clearTimeout(f.t);f.t=0}},
    pause(){f.on=false;f.arm()}};
  el.addEventListener("scroll",()=>{const up=el.scrollTop<f.last-1;f.last=el.scrollTop;
    if(f.on){if(up&&!atBottom(el))f.pause()}  // only moving up stops following
    else if(atBottom(el)||up)f.arm()});
  el.addEventListener("wheel",ev=>{if(ev.deltaY<0&&el.scrollHeight>el.clientHeight)f.pause()},{passive:true});  // before a new message can pull it back down
  el.addEventListener("pointerdown",ev=>{if(ev.target===el&&ev.offsetX>el.clientWidth)f.pause()});  // grabbing the scrollbar
  el.addEventListener("load",()=>f.stick(),true);  // late images
  new ResizeObserver(()=>f.stick()).observe(el);
  return f}
const LV=Object.assign(follower($("taskinfo")),{shown:null}),FL=follower($("events")),NB=Object.assign(follower($("notes")),{shown:null});  // notes board follows new notes too
function stickLive(){LV.stick()}
async function loadTask(){const f=$("filter").value;
  if(!f){$("taskinfo").innerHTML='<div class="sub">Pick a task above to see its sub-task tree and results.</div>';taskButtons(null);return}
  const r=await fetch(`/api/tasks/${encodeURIComponent(f)}`);const t=r.ok?await r.json():null;
  if(t)state.status[f]=t.status;const box=$("taskinfo"),top=box.scrollTop;if(LV.shown!==f){LV.shown=f;LV.resume()}
  box.innerHTML=t?node(t):"";if(!LV.on)box.scrollTop=LV.last=top;taskButtons(t);if(window.MD)MD.enhance(box);
  stickLive();LV.ro.disconnect();if(box.firstElementChild)LV.ro.observe(box.firstElementChild)}  // late growth: images, diagrams, expanded nodes
LV.ro=new ResizeObserver(stickLive);
const ACTIVE=new Set(["pending","running","needs_attention"]);
function taskButtons(t){const run=!!t&&(ACTIVE.has(t.status)||t.status==="paused");
  $("tcancel").style.display=run?"":"none";$("tdel").style.display=t&&!run?"":"none";$("tarch").style.display=t&&!run&&!t.parent_id?"":"none";
  $("tpause").style.display=t&&ACTIVE.has(t.status)&&t.status!=="pending"?"":"none";
  $("tresume").style.display=t&&!ACTIVE.has(t.status)?"":"none";
  $("tresume").title=t&&t.status==="paused"?"Continue the paused task":"Continue this task in the same workspace";
  setMode(MODE)}
const loadTaskSoon=debounce(loadTask,200);
/* Claude SDK usage in the header (static/usage.js): the overall total and the plan limit chip */
async function loadUsage(){try{const r=await fetch("/api/usage");if(r.ok)usageHeader.render([await r.json()],$("usage"),$("ratelim"))}catch{}}
const usageSoon=debounce(loadUsage,1000);
function onEvent(e,live){
  if(state.seen.has(e.id))return;state.seen.add(e.id);
  if(pendingSeen(e))clearPending();
  if(live&&e.kind==="llm"&&e.meta&&(e.meta.session_usage||e.meta.rate_limit))usageSoon();  // a run ended or a plan limit changed
  if(e.kind==="task"&&!e.parent_task_id&&(e.meta||{}).schedule)state.sched.add(e.task_id);
  state.events.push(e);if(e.task_name)state.names[e.task_id]=(state.sched.has(e.task_id)?"⏱ ":"")+e.task_name;
  if(e.kind==="task"){addTask(e.task_id,state.names[e.task_id]);state.lastRoot=e.task_id;state.status[e.task_id]="running"}
  if(e.kind==="done"&&e.task_id===e.root_task_id)state.status[e.task_id]=(e.meta||{}).status||"done";
  if(e.kind==="task_deleted")dropTask(e.task_id);
  if(e.kind==="task_paused")state.status[e.task_id]="paused";
  if(["task_resumed","task_reopened"].includes(e.kind))state.status[e.task_id]="running";
  if(e.kind==="task_restored"&&!(e.meta||{}).archived){bringBack(e.task_id)}
  if(e.kind==="task_archived"){if(live)state.archived.add(e.task_id);if(state.archived.has(e.task_id))dropTask(e.task_id)}
  if(e.kind==="task_unarchived"&&live&&state.archived.delete(e.task_id))bringBack(e.task_id);
  if(e.kind==="attention_requested"&&live&&e.meta.timeout>0)addAttention(e.meta,true);  // unattended runs: answered at once
  if(e.kind==="attention_resolved")removeAttention(e.meta.id);
  if(live&&["attention_requested","attention_resolved","error","done","alert_acked","task_deleted","task_archived","task_reopened","task_resumed"].includes(e.kind))loadAlertsSoon();
  if(e.kind==="limits"&&live&&e.meta&&"active" in e.meta)setLimits(e.meta);
  const d=R.on?null:render(e);if(d){$("events").append(d);FL.soon()}
  if(live){if(!R.on)animate(e);
    if(["task","direct","handover","done","llm","attention_requested","attention_resolved","error","departments"].includes(e.kind))loadAgentsSoon();
    if(["artifact","done"].includes(e.kind))loadArtifactsSoon();
    if(e.kind==="memory"&&e.root_task_id===noteRoot())loadNotes();
    if(e.root_task_id&&e.root_task_id===$("filter").value&&["task","reply","result","error","done","attention_requested","attention_resolved","handover","direct","task_cancelled","task_paused","task_resumed","task_reopened"].includes(e.kind))loadTaskSoon();
    if(["task_paused","task_resumed"].includes(e.kind))loadAgentsSoon();
    if($("dlg").style.display==="flex"&&$("dlg").dataset.kind==="manage"&&["task","done","task_restored","task_archived","task_unarchived"].includes(e.kind))drawManageSoon();
    for(const f of XQO.hooks.event)try{f(e)}catch(err){console.error("plugin UI",err)}  // plugins' own UI (XQO.on)
    if(e.kind==="done"&&e.sender&&!state.agents.find(a=>a.name===e.sender&&a.active>1))delete state.activity[e.sender];
  }
}

