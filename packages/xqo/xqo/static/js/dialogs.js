/* Replay, the shared dialog, fork & git */
/* ---------------- replay ---------------- */
function replayLoad(){const open={},att={},act={};
  for(const e of R.ev.slice(0,R.pos+1)){
    if(["task","direct","handover"].includes(e.kind)&&e.recipient)open[e.task_id]=e.recipient;
    else if(e.kind==="done")delete open[e.task_id];
    if(e.kind==="attention_requested")att[e.meta.id]=e.meta.agent||e.sender;
    else if(e.kind==="attention_resolved")delete att[e.meta.id];
  }
  for(const a of Object.values(open))act[a]=(act[a]||0)+1;return {act,att}}
async function startReplay(){
  const f=$("filter").value||state.lastRoot;if(!f){alert("Start or pick a task first.");return}
  const r=await fetch(`/api/tasks/${encodeURIComponent(f)}/events`);if(!r.ok){alert("No events logged for this task.");return}
  R.ev=await r.json();R.root=f;R.on=true;R.pos=-1;
  if($("filter").value!==f){addTask(f,tname(f));$("filter").value=f;loadTask();loadNotes()}
  $("rpbar").classList.add("on");$("rpscrub").max=R.ev.length-1;$("rpbtn").textContent="■ exit replay";
  $("flowh").textContent=`Message flow · replay of ${tname(f)}`;seek(0,true);
}
function stopReplay(){pause();R.on=false;R.ev=[];$("rpbar").classList.remove("on");$("rpbtn").textContent="▶ replay";
  $("flowh").textContent="Message flow";state.activity={};$("fx").innerHTML="";rerender();updateGraph()}
function seek(i,anim){
  if(!R.ev.length)return;i=Math.max(0,Math.min(R.ev.length-1,i));
  const fwd=i===R.pos+1;if(!fwd)$("fx").innerHTML="";R.pos=i;state.activity={};
  for(const e of R.ev.slice(0,i+1)){if(ACT[e.kind]&&state.pos[e.sender])state.activity[e.sender]=ACT[e.kind];if(e.kind==="done")delete state.activity[e.sender]}
  drawReplay();if(anim&&fwd)animate(R.ev[i]);else updateGraph();
}
function drawReplay(){
  const box=$("events");box.innerHTML="";let cur=null;
  R.ev.slice(0,R.pos+1).forEach((e,i)=>{const d=render(e);if(d){if(i===R.pos){d.classList.add("cur","open");cur=d}box.append(d)}});
  const e=R.ev[R.pos];$("rpscrub").value=R.pos;
  $("rppos").textContent=e?`${R.pos+1} / ${R.ev.length} · ${e.kind} · ${e.ts.slice(11,23)}`:"";
  $("rpplay").textContent=R.timer?"⏸ pause":"▶ play";if(cur)FL.resume();
}
function play(){if(R.pos>=R.ev.length-1)seek(0,true);R.timer=true;tick()}
function pause(){if(R.timer&&R.timer!==true)clearTimeout(R.timer);R.timer=null;if(R.on)drawReplay()}
function tick(){if(!R.timer)return;if(R.pos>=R.ev.length-1){pause();return}
  seek(R.pos+1,true);const next=R.ev[R.pos+1],skip=next&&next.kind==="llm"&&$("hidellm").checked;
  R.timer=setTimeout(tick,skip?30:900/+$("rpspeed").value);$("rpplay").textContent="⏸ pause"}
$("rpbtn").onclick=()=>R.on?stopReplay():startReplay();
$("rpplay").onclick=()=>R.timer?pause():play();
$("rpnext").onclick=()=>{pause();seek(R.pos+1,true)};$("rpprev").onclick=()=>{pause();seek(R.pos-1,false)};
$("rpfirst").onclick=()=>{pause();seek(0,true)};$("rplast").onclick=()=>{pause();seek(R.ev.length-1,false)};
$("rpscrub").oninput=()=>{pause();seek(+$("rpscrub").value,false)};
$("rpfork").onclick=()=>{pause();if(R.ev[R.pos])openFork(R.ev[R.pos])};
document.addEventListener("keydown",e=>{if(!R.on||/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)||$("dlg").style.display==="flex")return;
  if(e.key==="ArrowRight"){e.preventDefault();$("rpnext").click()}else if(e.key==="ArrowLeft"){e.preventDefault();$("rpprev").click()}
  else if(e.key===" "){e.preventDefault();$("rpplay").click()}});

/* ---------------- dialog: fork & git ---------------- */
function dialog(title,html,kind=""){$("dtitle").textContent=title;$("dbody").innerHTML=html;$("dlg").dataset.kind=kind;$("dlg").style.display="flex"}
const closeDialog=()=>{$("dlg").style.display="none"};
$("dclose").onclick=closeDialog;$("dlg").onclick=e=>{if(e.target===$("dlg"))closeDialog()};
document.addEventListener("keydown",e=>{if(e.key==="Escape")closeDialog()});
async function openFork(e){
  const root=e.root_task_id,r=await fetch(`/api/tasks/${encodeURIComponent(root)}`),t=r.ok?await r.json():null;
  const prompt=t?t.prompt:(state.events.find(x=>x.kind==="task"&&x.task_id===root)||{}).content||"";
  const entry=t?t.assignee:"";
  dialog("Fork task",`<div class="sub">Branch <b>${esc(tname(root))}</b> right after this event. The new task starts with the workspace files and notes board as they were at that point, and gets the earlier message flow as context.</div>
    <div class="ev open" style="--k:var(--${esc(e.kind)})"><div class="h"><span class="kind">${esc(e.kind.replace("_"," "))}</span><span class="who">${esc(nick(e.sender))}</span>${e.recipient?` → <span class="who">${esc(nick(e.recipient))}</span>`:""}<span class="ts">${esc(e.ts.slice(11,23))}</span></div><div class="body" style="max-height:8em;overflow:auto">${esc((e.content||"").slice(0,2000))}</div></div>
    <label for="fprompt">Prompt for the new branch (edit it to change direction)</label><textarea id="fprompt" rows="6">${esc(prompt)}</textarea>
    <label for="fagent">Entry agent</label>
    <div class="row" style="margin-top:0"><select id="fagent">${state.agents.map(a=>`<option value="${esc(a.name)}" ${a.name===entry?"selected":""}>${esc(nick(a.name))}</option>`).join("")}</select><button id="fgo" style="width:140px">⑂ Fork and run</button></div>`);
  $("fgo").onclick=async()=>{$("fgo").disabled=true;
    const res=await fetch(`/api/tasks/${encodeURIComponent(root)}/fork`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({event_id:e.id,prompt:$("fprompt").value,agent:$("fagent").value})});
    if(!res.ok){$("fgo").disabled=false;alert(await res.text());return}
    const nt=await res.json();closeDialog();if(R.on)stopReplay();
    state.names[nt.id]=nt.name;addTask(nt.id,nt.name);$("filter").value=nt.id;rerender();loadTask();loadNotes();loadArtifactsSoon()};
}
async function openGit(root){
  const [log,cfg]=await Promise.all([fetch(`/api/git/${encodeURIComponent(root)}/log`).then(r=>r.json()),fetch("/api/config").then(r=>r.json())]);
  const g=cfg.git||{};
  if(!log.enabled){dialog(`Git · ${tname(root)}`,'<div class="sub">Git history is off (git missing, or "git": {"enabled": false} in agents.json).</div>');return}
  dialog(`Git · ${tname(root)}`,`<div class="sub">Every workspace version is a commit, authored by the agent that wrote it. Repo: <code>artifacts/${esc(root)}/workspace/.git</code></div>
    <label>Push to a remote</label>
    <div class="row" style="margin-top:0"><input id="gremote" placeholder="https://github.com/you/repo.git" value="${esc(g.remote||"")}"><input id="gbranch" style="width:140px" value="${esc(g.branch||"main")}"><button id="gpush" style="width:90px">Push</button></div>
    <div class="tid" style="margin-top:4px">${g.token_set?`Token from $${esc(g.token_env)} will be used for https remotes.`:`No token: set $${esc(g.token_env||"XQO_GIT_TOKEN")} before starting the server to push over https (it is never stored or logged).`}</div>
    <div class="tid" id="gout" style="white-space:pre-wrap;margin-top:6px"></div>
    <label>${log.commits.length} commit${log.commits.length===1?"":"s"}</label>
    ${log.commits.map(c=>`<div class="cm"><code>${esc(c.sha.slice(0,8))}</code> <b>${esc(c.author)}</b> ${esc(c.message)} <span class="tid">${esc(c.date.replace("T"," ").slice(0,19))}</span>
      <div class="tid">${c.files.map(f=>`${esc(f.status)} ${esc(f.file)}`).join(" · ")}</div></div>`).join("")||'<div class="sub">No commits yet.</div>'}`);
  $("gpush").onclick=async()=>{$("gpush").disabled=true;$("gout").textContent="pushing…";
    const r=await fetch(`/api/git/${encodeURIComponent(root)}/push`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({remote_url:$("gremote").value,branch:$("gbranch").value})});
    const d=await r.json().catch(()=>({}));$("gpush").disabled=false;
    $("gout").textContent=r.ok?`✓ pushed ${d.head.slice(0,8)} to ${d.remote} (${d.branch})`:`✗ ${d.detail||r.status}`};
}

