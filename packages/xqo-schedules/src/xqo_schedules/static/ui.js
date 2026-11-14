/* schedules plugin UI: the ⏱ schedules button and dialog (loaded by the dashboard when the plugin is active) */
/* ---------------- schedules: scheduled and repeating tasks (each run is a normal task) ---------------- */
const S={view:"list",edit:null,when:"daily",files:[]};
const DAYS=[["1","Mon"],["2","Tue"],["3","Wed"],["4","Thu"],["5","Fri"],["6","Sat"],["0","Sun"]];
const browserTz=()=>{try{return Intl.DateTimeFormat().resolvedOptions().timeZone||""}catch{return ""}};
function rel(iso){if(!iso)return "–";const d=new Date(iso),s=(d-Date.now())/1000,a=Math.abs(s);
  const t=a<90?`${Math.round(a)}s`:a<5400?`${Math.round(a/60)}m`:a<129600?`${Math.round(a/3600)}h ${Math.round(a%3600/60)}m`:`${Math.round(a/86400)}d`;
  return `${s>=0?"in ":""}${t}${s<0?" ago":""}`}
const local=iso=>iso?new Date(iso).toLocaleString([], {weekday:"short",month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"}):"";
function openSchedules(view="list",edit=null){S.view=view;S.edit=edit;S.files=[];dialog("Schedules",'<div id="sbody"></div>',"schedules");drawSchedules()}
async function drawSchedules(){
  if($("dlg").dataset.kind!=="schedules"||$("dlg").style.display!=="flex")return;
  const box=$("sbody");if(!box)return;
  if(S.view==="form")return drawScheduleForm(box);
  if(S.view==="runs")return drawScheduleRuns(box);
  let r;try{r=await api("/api/schedules")}catch(err){box.innerHTML=`<div class="sub">${esc(err.message)}</div>`;return}
  box.innerHTML=`<div class="mbar" style="margin:0 0 6px"><span class="sub" style="margin:0">Each run starts a normal task in the task list (⏱). Upcoming runs are listed here.</span><span class="spacer"></span><button id="snew">+ New schedule</button></div>`+
    (r.warning?`<div class="sub" style="color:var(--attention)">${esc(r.warning)}</div>`:!r.running?`<div class="sub" style="color:var(--attention)">The scheduler is not running in this process.</div>`:"")+
    (r.items.map(s=>{const state=s.finished?"finished":!s.enabled?"paused":s.running.length?"running":"";
      return `<div class="srow${s.enabled?"":" off"}" data-id="${esc(s.id)}">
      <input type="checkbox" data-tog ${s.enabled?"checked":""} ${s.finished&&s.trigger.kind==="once"?"disabled":""} title="${s.enabled?"Pause":"Resume"} this schedule">
      <span class="nm" title="${esc(s.prompt)}">${s.source!=="ui"?`<span title="${s.source==="feed"?"Started by a feed rule (feed_rules in agents.json)":"Defined in agents.json: change it there"}">${s.source==="feed"?"📡":"🔒"}</span> `:""}${esc(s.name)}</span>
      <span class="acts"><button class="ghost" data-run title="Start a run now">▶ Run now</button>${s.source!=="ui"?"":'<button class="ghost" data-edit>Edit</button>'}<button class="ghost" data-runs>History</button>${s.source!=="ui"?"":'<button class="ghost danger" data-del>Delete</button>'}</span>
      <span class="meta"><span>${esc(s.summary)}</span><span>agent: ${esc(s.agent||"default")}</span>
        <span>${s.next_run?`next: <b title="${esc(s.next_run)}">${esc(local(s.next_run))}</b> (${rel(s.next_run)})`:`<b>${state||"no next run"}</b>`}</span>
        <span>${s.last_run?`last: <span class="st ${esc(s.last_status==="cost_cap"?"error":s.last_status||"")}">${esc((s.last_status||"").replace("_"," "))}</span> ${rel(s.last_run)}${s.last_task?` · <a href="#" data-task="${esc(s.last_task)}">open</a>`:""}`:"never ran"}</span>
        <span>${s.runs} run${s.runs===1?"":"s"}</span>${s.queued?"<span>1 queued</span>":""}${s.running.length?`<span class="st running">running</span>`:""}
        ${s.overlap!=="skip"?`<span>overlap: ${esc(s.overlap)}</span>`:""}${s.limits.max_cost_usd?`<span>cap $${s.limits.max_cost_usd}</span>`:""}${s.unattended?"":"<span>attended</span>"}</span>
      ${s.paused_reason?`<span class="warnline">${esc(s.paused_reason)}</span>`:""}${s.agent_missing?`<span class="warnline">agent ${esc(s.agent)} does not exist</span>`:""}</div>`}).join("")||'<div class="sub">No schedules yet. Use “+ New schedule”, or add "schedules" to agents.json.</div>');
  $("snew").onclick=()=>{S.view="form";S.edit=null;S.files=[];drawSchedules()};
  const sid=el=>el.closest(".srow").dataset.id,item=id=>r.items.find(s=>s.id===id);
  box.querySelectorAll("[data-tog]").forEach(c=>c.onchange=async()=>{try{await api(`/api/schedules/${encodeURIComponent(sid(c))}/${c.checked?"resume":"pause"}`,{method:"POST"})}catch(err){alert(err.message)}drawSchedules()});
  box.querySelectorAll("[data-run]").forEach(b=>b.onclick=async()=>{b.disabled=true;try{const x=await api(`/api/schedules/${encodeURIComponent(sid(b))}/run`,{method:"POST"});
    if(x.task_id)toast(`⏱ ${x.schedule.name}: run started`,null,{label:"Open",run:()=>openRun(x.task_id)})}
    catch(err){alert(err.message)}drawSchedules()});
  box.querySelectorAll("[data-edit]").forEach(b=>b.onclick=()=>{S.view="form";S.edit=item(sid(b));S.files=[];drawSchedules()});
  box.querySelectorAll("[data-runs]").forEach(b=>b.onclick=()=>{S.view="runs";S.edit=item(sid(b));drawSchedules()});
  box.querySelectorAll("[data-del]").forEach(b=>b.onclick=async()=>{const s=item(sid(b));if(!confirm(`Delete the schedule ${s.name}?\n\nTasks it already started are kept.`))return;
    try{await api(`/api/schedules/${encodeURIComponent(s.id)}`,{method:"DELETE"})}catch(err){alert(err.message)}drawSchedules()});
  box.querySelectorAll("[data-task]").forEach(a=>a.onclick=ev=>{ev.preventDefault();openRun(a.dataset.task)})}
function openRun(tid){closeDialog();if(state.archived.has(tid)){if(confirm("This run is archived. Unarchive it to open it?"))archiveTasks([tid],false).then(()=>setTimeout(()=>selectTask(tid),400));return}selectTask(tid)}
const drawSchedulesSoon=debounce(drawSchedules,300);
async function drawScheduleRuns(box){const s=S.edit;let runs=[];try{runs=await api(`/api/schedules/${encodeURIComponent(s.id)}/runs?limit=200`)}catch(err){alert(err.message)}
  box.innerHTML=`<div class="mbar" style="margin:0 0 8px"><button class="ghost" id="sback">← Schedules</button><b>${esc(s.name)}</b><span class="tid">${esc(s.summary)}</span></div>`+
    (runs.map(e=>{const m=e.meta||{};return `<div class="mrow"><span class="tid" style="width:130px;flex:none">${esc(local(e.ts))}</span>
      <span class="st ${esc(m.outcome==="started"?"running":m.outcome==="cost_cap"?"error":m.outcome||"")}">${esc((m.outcome||e.kind.replace("schedule_","")).replace("_"," "))}</span>
      <span class="nm" title="${esc(e.content)}">${esc(e.content)}</span>${e.task_id?`<a href="#" class="ghost" data-task="${esc(e.task_id)}">open</a>`:""}</div>`}).join("")||'<div class="sub">No runs yet.</div>');
  $("sback").onclick=()=>{S.view="list";drawSchedules()};
  box.querySelectorAll("[data-task]").forEach(a=>a.onclick=ev=>{ev.preventDefault();openRun(a.dataset.task)})}
/* the form: once / every / daily at times on weekdays / cron; times are wall-clock times in the time zone */
const inputTime=iso=>{if(!iso)return "";const d=new Date(iso);return new Date(d-d.getTimezoneOffset()*60000).toISOString().slice(0,16)};  // datetime-local, browser time
function cronParts(expr){const m=/^(\d{1,2}) ([\d,]+) \* \* ([\d,\-*]+)$/.exec(expr||"");if(!m)return null;
  let days=new Set();if(m[3]==="*")days=new Set(["0","1","2","3","4","5","6"]);else for(const p of m[3].split(",")){const [a,b]=p.split("-").map(Number);if(isNaN(a))return null;for(let d=a;d<=(isNaN(b)?a:b);d++)days.add(String(d%7))}
  return {time:m[2].split(",").map(h=>`${h.padStart(2,"0")}:${m[1].padStart(2,"0")}`).join(", "),days}}
function drawScheduleForm(box){const s=S.edit||{},t=s.trigger||{},lim=s.limits||{},end=s.end||{};
  const cp=t.kind==="cron"?cronParts(t.expr):null;
  S.when=s.id?(t.kind==="once"?"once":t.kind==="interval"?"every":cp?"daily":"cron"):"daily";
  const every=t.kind==="interval"?(t.every%86400===0?`${t.every/86400}d`:t.every%3600===0?`${t.every/3600}h`:`${Math.round(t.every/60)}m`):"6h";
  const atLocal=t.kind==="once"?inputTime(t.at):"";
  const opts=state.agents.map(a=>`<option value="${esc(a.name)}" ${a.name===s.agent?"selected":""}>${esc(a.name)}</option>`).join("");
  const dur=v=>v?(v%86400===0?`${v/86400}d`:v%3600===0?`${v/3600}h`:`${Math.round(v/60)}m`):"";
  box.innerHTML=`<div class="sform"><div class="mbar" style="margin:0"><button class="ghost" id="sback">← Schedules</button><b>${s.id?"Edit schedule":"New schedule"}</b></div>
    <div class="g2"><div><label>Name</label><input id="sname" maxlength="120" value="${esc(s.name||"")}" placeholder="Nightly dependency check"></div>
      <div><label>Agent</label><select id="sagent"><option value="">default (${esc((state.agents[0]||{}).name||"first agent")})</option>${opts}</select></div></div>
    <label>Prompt</label><textarea id="sprompt" rows="4" placeholder="What each run should do…">${esc(s.prompt||"")}</textarea>
    ${s.id?(s.attachments||[]).length?`<div class="hint">Attachments: ${s.attachments.map(a=>esc(a.name)).join(", ")}</div>`:"":`<label>Attachments <span class="hint">(copied into every run)</span></label><input type="file" id="sfiles" multiple>`}
    <label>When</label><div class="when">${[["once","Once"],["every","Every"],["daily","Daily / weekdays"],["cron","Cron"]].map(([k,l])=>`<button class="ghost${S.when===k?" on":""}" data-when="${k}">${l}</button>`).join("")}</div>
    <div id="w-once" class="g2"><div><label>Date and time</label><input type="datetime-local" id="sat" value="${esc(atLocal)}"></div></div>
    <div id="w-every" class="g2"><div><label>Every</label><input id="severy" value="${esc(every)}" placeholder="30m, 6h, 1d"><div class="hint">s, m, h, d or w; 1 minute at least</div></div></div>
    <div id="w-daily"><div class="g2"><div><label>Time(s)</label><input id="stimes" value="${esc(cp?cp.time:"09:00")}" placeholder="09:00, 17:30"></div></div>
      <label>Days</label><div class="days">${DAYS.map(([v,l])=>`<label><input type="checkbox" value="${v}" ${(!cp&&+v>=1&&+v<=5)||(cp&&cp.days.has(v))?"checked":""}>${l}</label>`).join("")}</div></div>
    <div id="w-cron" class="g2"><div><label>Cron</label><input id="scron" value="${esc(t.kind==="cron"?t.expr:"0 9 * * 1-5")}"><div class="hint">minute hour day-of-month month day-of-week</div></div></div>
    <div class="g2"><div><label>Time zone</label><input id="stz" value="${esc(s.id?(s.tz||""):browserTz())}" placeholder="server time"></div></div>
    <details ${s.id?"open":""}><summary>Runs, limits and clean-up</summary>
      <div class="g3"><div><label>If the last run is still going</label><select id="soverlap">${[["skip","skip this run"],["queue","queue one run"],["parallel","run in parallel"]].map(([v,l])=>`<option value="${v}" ${(s.overlap||"skip")===v?"selected":""}>${l}</option>`).join("")}</select></div>
        <div><label>Missed while the server was down</label><select id="smissed">${[["run_once","run once (within the grace)"],["skip","skip"]].map(([v,l])=>`<option value="${v}" ${(s.missed||"run_once")===v?"selected":""}>${l}</option>`).join("")}</select></div>
        <div><label>Grace</label><input id="sgrace" value="${esc(dur(s.grace)||"1h")}"></div></div>
      <div class="g3"><div><label>Max turns</label><input id="sturns" type="number" min="1" value="${esc(lim.max_turns||"")}" placeholder="agent's"></div>
        <div><label>Max tokens (output)</label><input id="stokens" type="number" min="1" value="${esc(lim.max_tokens||"")}" placeholder="agent's"></div>
        <div><label>Max cost per run ($)</label><input id="scost" type="number" min="0" step="0.01" value="${esc(lim.max_cost_usd||"")}" placeholder="none"></div></div>
      <label class="chk"><input type="checkbox" id="sunatt" ${s.unattended===false?"":"checked"}> Unattended: questions to you take their default answer at once</label>
      <label class="chk"><input type="checkbox" id="shonor" ${lim.honor_no_limits?"checked":""}> The “No limits” hour applies to these runs too</label>
      <div class="g3"><div><label>Pause after failed runs in a row</label><input id="sfails" type="number" min="0" value="${esc(s.max_failures??3)}"><div class="hint">0 = never</div></div>
        <div><label>Stop after runs</label><input id="sruns" type="number" min="1" value="${esc(end.after_runs||"")}" placeholder="never"></div>
        <div><label>Stop after</label><input type="datetime-local" id="suntil" value="${esc(inputTime(end.until))}"></div></div>
      <div class="g3"><div><label>Archive finished runs after</label><input id="sarch" value="${esc(dur(s.archive_after))}" placeholder="never, e.g. 7d"></div></div>
    </details>
    <div class="err" id="serr"></div>
    <div class="mbar"><label class="chk" style="margin:0"><input type="checkbox" id="senabled" ${s.enabled===false?"":"checked"}> enabled</label><span class="spacer"></span><button class="ghost" id="scancel">Cancel</button><button id="ssave" style="width:auto;padding:4px 16px">${s.id?"Save":"Create"}</button></div></div>`;
  const show=()=>{for(const k of ["once","every","daily","cron"])$("w-"+k).style.display=S.when===k?"":"none";
    box.querySelectorAll("[data-when]").forEach(b=>b.classList.toggle("on",b.dataset.when===S.when))};
  box.querySelectorAll("[data-when]").forEach(b=>b.onclick=()=>{S.when=b.dataset.when;show()});show();
  $("sback").onclick=$("scancel").onclick=()=>{S.view="list";drawSchedules()};
  $("ssave").onclick=async()=>{const err=$("serr");err.textContent="";
    const body={name:$("sname").value.trim(),prompt:$("sprompt").value.trim(),agent:$("sagent").value||null,tz:$("stz").value.trim()||null,
      overlap:$("soverlap").value,missed:$("smissed").value,grace:$("sgrace").value.trim()||"1h",unattended:$("sunatt").checked,
      max_failures:+$("sfails").value||0,enabled:$("senabled").checked,archive_after:$("sarch").value.trim()||null};
    if(S.when==="once"){if(!$("sat").value){err.textContent="Pick a date and time.";return}body.trigger={kind:"once",at:$("sat").value}}
    else if(S.when==="every")body.trigger={kind:"interval",every:$("severy").value.trim()};
    else if(S.when==="cron")body.trigger={kind:"cron",expr:$("scron").value.trim()};
    else{const times=$("stimes").value.split(/[,\s]+/).filter(Boolean),days=[...box.querySelectorAll(".days input:checked")].map(c=>c.value);
      if(!times.length||!days.length){err.textContent="Give at least one time and one day.";return}
      const hm=times.map(x=>/^(\d{1,2}):(\d{2})$/.exec(x));if(hm.some(m=>!m)){err.textContent="Times look like 09:00.";return}
      const mins=new Set(hm.map(m=>+m[2]));if(mins.size>1){err.textContent="Several times must share their minutes (09:00, 17:00), or use Cron.";return}
      const ds=days.map(Number).sort((a,b)=>a-b),runs=[];for(const d of ds){const r=runs[runs.length-1];if(r&&d===r[1]+1)r[1]=d;else runs.push([d,d])}
      body.trigger={kind:"cron",expr:`${[...mins][0]} ${[...new Set(hm.map(m=>+m[1]))].sort((a,b)=>a-b).join(",")} * * ${days.length===7?"*":runs.map(([a,b])=>a===b?a:b===a+1?`${a},${b}`:`${a}-${b}`).join(",")}`}}
    const limits={};if($("sturns").value)limits.max_turns=+$("sturns").value;if($("stokens").value)limits.max_tokens=+$("stokens").value;
    if($("scost").value)limits.max_cost_usd=+$("scost").value;if($("shonor").checked)limits.honor_no_limits=true;body.limits=limits;
    body.end={...($("sruns").value?{after_runs:+$("sruns").value}:{}),...($("suntil").value?{until:$("suntil").value}:{})};
    const fi=$("sfiles");if(fi&&fi.files.length){try{body.attachments=await Promise.all([...fi.files].map(async f=>({name:f.name,media_type:f.type||null,data:await b64(f)})))}catch(e){err.textContent=String(e);return}}
    $("ssave").disabled=true;
    try{await api(s.id?`/api/schedules/${encodeURIComponent(s.id)}`:"/api/schedules",{method:s.id?"PATCH":"POST",body:JSON.stringify(body)});S.view="list";drawSchedules()}
    catch(e){err.textContent=e.message}finally{const b=$("ssave");if(b)b.disabled=false}}}
setInterval(()=>{if($("dlg").style.display==="flex"&&$("dlg").dataset.kind==="schedules"&&S.view==="list")drawSchedules()},30000);  // keep "in 5m" fresh
XQO.button({id:"tsched",label:"⏱ schedules",title:"Scheduled and repeating tasks",place:"tasks",onclick:()=>openSchedules()});
XQO.on(e=>{
    if(e.kind.startsWith("schedule_")){if($("dlg").style.display==="flex"&&$("dlg").dataset.kind==="schedules")drawSchedulesSoon();
      if(e.kind==="schedule_paused"&&(e.meta||{}).alert)toast(`⏱ ${e.content}`,null)}
});
