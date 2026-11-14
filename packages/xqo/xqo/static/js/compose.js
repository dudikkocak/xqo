/* New task | Ask | Steer, cancel / delete / archive, the tasks manager */
/* ---------------- New task | Ask | Steer ---------------- */
let MODE="task";
const PH={task:"Describe the task… Paste or drop files and images here.",ask:"Ask a question… Start with @agent to pick who answers. Paste or drop files here.",
  steer:"Give new instructions, e.g. “use FastAPI instead”… Start with @agent to pick who. Paste or drop files here."};
function fillTo(){const cur=$("to").value;
  $("to").innerHTML=`<option value="">all · entry agent</option>`+chatTargets().map(([v,l])=>`<option value="${esc(v)}" ${v===cur?"selected":""}>${esc(l)}</option>`).join("")}
function chatTargets(){  // departments first (their head handles it), then every agent
  const depts=(state.depts||[]).filter(d=>d.name!=="hq"&&d.head).map(d=>[d.name,`@${d.name} · department (head ${d.head})`]);
  return depts.concat(state.agents.map(a=>[a.name,"@"+a.name+(a.nickname?` (${a.nickname})`:"")+(state.deptOf&&state.deptOf[a.name]?` · ${state.deptOf[a.name]}`:"")]))}
function setMode(m){
  const sel=$("filter").value,st=state.status[sel];
  for(const b of document.querySelectorAll(".tab[data-mode]")){b.classList.toggle("on",b.dataset.mode===m);if(b.dataset.mode!=="task")b.disabled=!sel}
  if(m!=="task"&&!sel)m="task";
  MODE=m;const chat=m!=="task";
  for(const b of document.querySelectorAll(".tab[data-mode]"))b.classList.toggle("on",b.dataset.mode===m);
  $("prompt").placeholder=PH[m];$("entry").style.display=chat?"none":"";$("to").style.display=chat?"":"none";
  $("send").textContent=chat?(m==="ask"?"Ask":"Steer"):"Submit";
  if(chat&&!$("to").options.length)fillTo();
  const tg=$("target");tg.style.display=chat||($("urgent").checked&&sel)?"":"none";tg.classList.remove("warn");
  if(chat){const live=ACTIVE.has(st)||st==="paused";tg.textContent=`→ ${tname(sel)}`+(live?"":" (finished: your message reopens it)");}
  else if($("urgent").checked&&sel){const live=ACTIVE.has(st);tg.textContent=live?`⏸ will pause ${tname(sel)} first`:"nothing selected is running: starts normally";tg.classList.toggle("warn",live)}
  $("urgl").classList.toggle("on",$("urgent").checked)}
document.querySelectorAll(".tab[data-mode]").forEach(b=>b.onclick=()=>{setMode(b.dataset.mode);$("prompt").focus()});
$("urgent").onchange=()=>setMode(MODE);
async function sendChat(text){
  let to=$("to").value||null;const m=text.match(/^@([\w.-]+)(?:\s+([\s\S]*))?$/);
  if(m&&chatTargets().some(([v])=>v===m[1])){to=m[1];text=m[2]||""}
  const attachments=await readAtts();
  const r=await api(`/api/tasks/${encodeURIComponent($("filter").value)}/chat`,{method:"POST",body:JSON.stringify({text,kind:MODE,to,urgent:$("urgent").checked,attachments})});
  clearAtts();if(r.reopened)state.status[$("filter").value]="running";return r}
async function pauseTask(id){try{await api(`/api/tasks/${encodeURIComponent(id)}/pause`,{method:"POST",body:"{}"});state.status[id]="paused";loadTask()}catch(err){alert(err.message)}}
/* Resume shows at once, like a New task / Ask / Steer: the button says so and the user node pulses until an agent responds */
async function resumeTask(id,btn){
  const pend=setPending("resume",id),label=btn&&btn.textContent;
  if(btn){btn.disabled=true;btn.classList.add("busy");btn.textContent="⏳ resuming…"}
  try{await api(`/api/tasks/${encodeURIComponent(id)}/resume`,{method:"POST"});pend.armed=true;state.status[id]="running";loadTask();drawManageSoon()}
  catch(err){clearPending(pend);alert(err.message)}
  finally{if(btn){btn.disabled=false;btn.classList.remove("busy");btn.textContent=label}}}
$("tpause").onclick=()=>$("filter").value&&pauseTask($("filter").value);
$("tresume").onclick=()=>$("filter").value&&resumeTask($("filter").value,$("tresume"));

/* ---------------- cancel & delete ---------------- */
const api=async(url,opt={})=>{const r=await fetch(url,{headers:{"Content-Type":"application/json"},...opt});
  const d=await r.json().catch(()=>null);if(!r.ok)throw new Error(d&&d.detail?d.detail:r.statusText);return d};
function dropTask(root){  // a task tree left (trash or purge): forget its events and its option
  state.events=state.events.filter(e=>{const mine=e.root_task_id===root&&!TOMB.has(e.kind);if(mine)state.seen.delete(e.id);return !mine});
  state.tasks.delete(root);delete state.status[root];
  const sel=$("filter").value===root,o=[...$("filter").options].find(o=>o.value===root);if(o)o.remove();
  if(R.on&&R.root===root)stopReplay();
  if(sel){$("filter").value="";loadTask();loadNotes()}
  rerender();loadArtifactsSoon()}
async function bringBack(root){
  try{const evs=await api(`/api/tasks/${encodeURIComponent(root)}/events`);for(const e of evs)onEvent(e,false);rerender();loadArtifactsSoon()}catch{}}
/* archive: finished task trees leave the task list and the message flow until unarchived (nothing is removed) */
async function archiveTasks(ids,on=true){
  if(!ids.length)return;
  try{const res=await api("/api/tasks/archive",{method:"POST",body:JSON.stringify({ids,archived:on})});
    const ok=res.filter(r=>r.ok).map(r=>r.id),bad=res.filter(r=>!r.ok);
    if(bad.length)alert(bad.map(r=>`${tname(r.id)}: ${r.error}`).join("\n"));
    if(on)for(const id of ok){state.archived.add(id);dropTask(id)}
    else for(const id of ok)if(state.archived.delete(id))bringBack(id);
    if(ok.length)toast(`${ok.length===1?tname(ok[0]):ok.length+" tasks"} ${on?"archived":"back in the task list"}`,null,on?{label:"Undo",run:()=>archiveTasks(ok,false)}:null);
    M.sel.clear();drawManageSoon()}
  catch(err){alert(err.message)}}
async function cancelTask(id){
  const reason=window.prompt(`Cancel ${tname(id)}?\n\nRunning agents, tests and open questions are stopped; files made so far are kept.\nReason (optional), then OK to cancel the task:`,"");
  if(reason===null)return;
  try{await api(`/api/tasks/${encodeURIComponent(id)}/cancel`,{method:"POST",body:JSON.stringify({reason})});loadTask();drawManageSoon()}
  catch(err){alert(err.message)}}
function branchesOf(ids,tasks){const set=new Set(ids);return tasks.filter(t=>!t.parent_id&&t.forked_from&&set.has(t.forked_from.task_id)&&!set.has(t.id))}
async function deleteTasks(ids,permanent){
  if(!ids.length)return;
  const tasks=await api("/api/tasks"),br=branchesOf(ids,tasks);
  const what=ids.length===1?tname(ids[0]):`${ids.length} tasks`;
  const warn=br.length?`\n\n${br.length} task${br.length===1?" was":"s were"} branched from ${ids.length===1?"it":"them"} and will be kept (they lose their link back):\n`+br.map(t=>"• "+t.name).join("\n"):"";
  const msg=permanent?`Permanently delete ${what}?\n\nIts files, history, git repo, notes, attachments and log entries are removed. This cannot be undone.`
    :`Delete ${what}?\n\nIt moves to the trash and can be restored for ${state.retention||7} days.`;
  if(!confirm(msg+warn))return;
  try{const res=await api("/api/tasks/delete",{method:"POST",body:JSON.stringify({ids,permanent})});
    const ok=res.filter(r=>r.ok).map(r=>r.id),bad=res.filter(r=>!r.ok);
    if(bad.length)alert(bad.map(r=>`${tname(r.id)}: ${r.error}`).join("\n"));
    ok.forEach(dropTask);if(ok.length&&!permanent)toast(`${ok.length===1?tname(ok[0]):ok.length+" tasks"} moved to the trash`,ok);
    else if(ok.length)toast(`${ok.length===1?"Task":ok.length+" tasks"} deleted permanently`,null);
    drawManageSoon()}
  catch(err){alert(err.message)}}
let toastTimer=null;
function toast(msg,undo,custom=null){$("tmsg").textContent=msg;$("tundo").style.display=undo||custom?"":"none";$("toast").style.display="flex";
  $("tundo").textContent=custom&&custom.label||"Undo";
  $("tundo").onclick=custom?()=>{$("toast").style.display="none";custom.run()}:
    async()=>{$("toast").style.display="none";for(const id of undo){try{await api(`/api/trash/${encodeURIComponent(id)}/restore`,{method:"POST"})}catch(err){alert(err.message)}}drawManageSoon()};
  clearTimeout(toastTimer);toastTimer=setTimeout(()=>$("toast").style.display="none",10000)}
$("tx").onclick=()=>$("toast").style.display="none";
$("tcancel").onclick=()=>$("filter").value&&cancelTask($("filter").value);
$("tdel").onclick=()=>$("filter").value&&deleteTasks([$("filter").value],false);
$("tarch").onclick=()=>$("filter").value&&archiveTasks([$("filter").value]);
const M={tab:"tasks",sel:new Set()};
async function drawManage(){
  if($("dlg").dataset.kind!=="manage"||$("dlg").style.display!=="flex")return;
  const box=$("mbody");if(!box)return;
  if(M.tab==="trash"){const tr=await api("/api/trash");state.retention=tr.retention_days;
    box.innerHTML=`<div class="sub">${tr.retention_days>0?`Deleted tasks are removed for good ${tr.retention_days} days after deletion.`:"Deleted tasks stay here until you remove them."}</div>`+
      (tr.items.map(e=>`<div class="mrow" data-id="${esc(e.root_id)}"><span class="nm" title="${esc(e.name)}">${esc(e.name)}</span>
        <span class="tid">${esc(e.deleted_at.replace("T"," ").slice(0,16))}${e.purge_at?` · removed ${new Date(e.purge_at*1000).toLocaleDateString()}`:""}</span>
        <button class="ghost" data-restore>Restore</button><button class="ghost danger" data-purge>Delete permanently</button></div>`).join("")||'<div class="sub">The trash is empty.</div>');
    box.querySelectorAll("[data-restore]").forEach(b=>b.onclick=async()=>{try{await api(`/api/trash/${encodeURIComponent(b.closest(".mrow").dataset.id)}/restore`,{method:"POST"})}catch(err){alert(err.message)}drawManage()});
    box.querySelectorAll("[data-purge]").forEach(b=>b.onclick=async()=>{const id=b.closest(".mrow").dataset.id,nm=b.closest(".mrow").querySelector(".nm").textContent;
      if(!confirm(`Permanently delete ${nm}? This cannot be undone.`))return;try{await api(`/api/trash/${encodeURIComponent(id)}`,{method:"DELETE"})}catch(err){alert(err.message)}drawManage()});
    return}
  if(M.tab==="archive"){const items=(await api("/api/archive")).items;
    for(const id of [...M.sel])if(!items.some(e=>e.root_id===id))M.sel.delete(id);
    box.innerHTML=`<div class="sub">Archived tasks keep their files, history and replay; they are only hidden from the task list. Asking or steering one brings it back.</div>`+
      (items.map(e=>`<div class="mrow" data-id="${esc(e.root_id)}"><input type="checkbox" ${M.sel.has(e.root_id)?"checked":""}>
        <span class="st ${esc(e.status)}">${esc(e.status.replace("_"," "))}</span><span class="nm" title="${esc(e.name)}">${e.schedule?"⏱ ":""}${esc(e.name)}</span>
        <span class="tid">${esc(e.at.replace("T"," ").slice(0,16))}${e.by&&e.by!=="user"?` · by ${esc(e.by)}`:""}</span>
        <button class="ghost" data-unarch>Unarchive</button></div>`).join("")||'<div class="sub">Nothing is archived.</div>')+
      (items.length?`<div class="mbar"><label style="margin:0;display:flex;gap:6px;align-items:center;color:var(--text)"><input type="checkbox" id="mall" style="width:auto"> select all</label><span class="spacer"></span>
        <span class="tid" id="mcount"></span><button class="ghost" id="munarch">Unarchive</button><button class="ghost danger" id="mdel">🗑 Delete</button></div>`:"");
    if(!items.length)return;
    const sync=()=>{$("mcount").textContent=M.sel.size?`${M.sel.size} selected`:"";$("munarch").disabled=$("mdel").disabled=!M.sel.size};
    box.querySelectorAll(".mrow input").forEach(c=>c.onchange=()=>{const id=c.closest(".mrow").dataset.id;c.checked?M.sel.add(id):M.sel.delete(id);sync()});
    box.querySelectorAll("[data-unarch]").forEach(b=>b.onclick=()=>archiveTasks([b.closest(".mrow").dataset.id],false));
    $("mall").onchange=()=>{for(const e of items)$("mall").checked?M.sel.add(e.root_id):M.sel.delete(e.root_id);drawManage()};
    $("munarch").onclick=()=>archiveTasks([...M.sel],false);$("mdel").onclick=()=>deleteTasks([...M.sel],false);sync();
    return}
  const roots=(await api("/api/tasks?archived=false")).filter(t=>!t.parent_id).sort((a,b)=>b.created.localeCompare(a.created));
  for(const id of [...M.sel])if(!roots.some(t=>t.id===id&&!ACTIVE.has(t.status)))M.sel.delete(id);
  box.innerHTML=(roots.map(t=>{const run=ACTIVE.has(t.status);return `<div class="mrow" data-id="${esc(t.id)}">
      <input type="checkbox" ${run?"disabled title=\"Cancel it first\"":""} ${M.sel.has(t.id)?"checked":""}>
      <span class="st ${esc(t.status)}">${esc(t.status.replace("_"," "))}</span><span class="nm" title="${esc(t.name)}">${t.schedule?"⏱ ":""}${esc(t.name)}</span>
      ${t.status==="paused"?'<button class="ghost" data-resume>▶ resume</button>':""}${run?'<button class="ghost warn" data-cancel>⏹ cancel</button>':""}</div>`}).join("")||'<div class="sub">No tasks yet.</div>')+
    `<div class="mbar"><label style="margin:0;display:flex;gap:6px;align-items:center;color:var(--text)"><input type="checkbox" id="mall" style="width:auto"> select all finished</label><span class="spacer"></span>
      <span class="tid" id="mcount"></span><button class="ghost" id="march" title="Hide from the task list; nothing is removed">🗄 Archive</button><button class="ghost danger" id="mdel">🗑 Delete</button><button class="dangerfill" id="mperm" style="padding:3px 10px;font-size:12px">Delete permanently</button></div>`;
  const sync=()=>{$("mcount").textContent=M.sel.size?`${M.sel.size} selected`:"";$("march").disabled=$("mdel").disabled=$("mperm").disabled=!M.sel.size};
  box.querySelectorAll(".mrow input").forEach(c=>c.onchange=()=>{const id=c.closest(".mrow").dataset.id;c.checked?M.sel.add(id):M.sel.delete(id);sync()});
  box.querySelectorAll("[data-cancel]").forEach(b=>b.onclick=()=>cancelTask(b.closest(".mrow").dataset.id));
  box.querySelectorAll("[data-resume]").forEach(b=>b.onclick=()=>resumeTask(b.closest(".mrow").dataset.id,b));
  $("mall").onchange=()=>{for(const t of roots)if(!ACTIVE.has(t.status))$("mall").checked?M.sel.add(t.id):M.sel.delete(t.id);drawManage()};
  $("mdel").onclick=()=>deleteTasks([...M.sel],false);$("mperm").onclick=()=>deleteTasks([...M.sel],true);$("march").onclick=()=>archiveTasks([...M.sel]);sync()}
const drawManageSoon=debounce(drawManage,250);
function openManage(tab="tasks"){if(M.tab!==tab)M.sel.clear();M.tab=tab;
  dialog("Tasks",`<div class="mt"><button class="ghost${tab==="tasks"?" on":""}" data-mtab="tasks">Tasks</button><button class="ghost${tab==="archive"?" on":""}" data-mtab="archive">🗄 Archived</button><button class="ghost${tab==="trash"?" on":""}" data-mtab="trash">🗑 Trash</button></div><div id="mbody"></div>`,"manage");
  $("dbody").querySelectorAll("[data-mtab]").forEach(b=>b.onclick=()=>openManage(b.dataset.mtab));drawManage()}
$("tmanage").onclick=()=>openManage();

