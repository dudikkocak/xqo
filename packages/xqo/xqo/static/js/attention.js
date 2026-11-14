/* Needs your attention, alerts, the no-limits hour, browser notifications */
/* ---------------- attention ---------------- */
function attStyle(){let css="";for(const a of Object.values(state.att))css+=`.ev[data-root="${CSS.escape(rootOf(a.task_id))}"]{box-shadow:inset 0 0 0 1px rgba(255,93,108,.5)}`;
  let s=$("attcss");if(!s){s=document.createElement("style");s.id="attcss";document.head.appendChild(s)}s.textContent=css}
function rootOf(tid){const e=state.events.find(x=>x.task_id===tid&&x.root_task_id);return e?e.root_task_id:tid}
function addAttention(m,notify){
  if(state.att[m.id])return;m._deadline=Date.now()+(m.remaining??m.timeout)*1000;state.att[m.id]=m;renderAtt();
  if(notify)alertUser(m);
}
function removeAttention(id){if(state.att[id]){delete state.att[id];renderAtt()}}
/* alerts (alerts.py): finished root tasks, errors and auto-answered questions blink until dismissed */
const NK_TXT={done:"finished · check",error:"error · check",auto_confirmed:"auto-answered · check"},NK_LBL={done:"task finished",error:"error",auto_confirmed:"auto-answered"};
const NK_RANK={error:3,auto_confirmed:2,done:1};
state.alerts=[];
function notices(){return (state.alerts||[]).filter(a=>a.kind!=="attention")}
function noticeKind(n){if(n==="user")return null;let best=null;for(const a of notices())if(a.agent===n&&(!best||NK_RANK[a.kind]>NK_RANK[best]))best=a.kind;return best}
async function loadAlerts(){try{const r=await api("/api/alerts");state.alerts=r.items||[];renderAtt()}catch(e){}}
const loadAlertsSoon=debounce(loadAlerts,200);
async function ackAlerts(body){try{await api("/api/alerts/ack",{method:"POST",body:JSON.stringify(body)})}catch(e){alert(e.message)}loadAlerts()}
function renderAtt(){
  const list=Object.values(state.att),nl=notices(),n=list.length+nl.length;
  $("attsec").style.display=n?"flex":"none";$("attcount").textContent=n?[list.length?`${list.length} pending`:"",nl.length?`${nl.length} to check`:""].filter(Boolean).join(" · "):"";
  $("attack").hidden=!nl.length;
  $("attbadge").style.display=n?"inline-block":"none";$("attbadge").textContent=`⚠ ${n} need${n===1?"s":""} attention`;
  $("attlist").innerHTML=list.map(a=>`<div class="attc" data-id="${esc(a.id)}">
    <div><span class="kb">${esc(a.reason)}</span> · <b>${esc(nick(a.agent))}</b> <span class="tid">${esc(tname(a.task_id))}</span></div>
    <div class="q">${esc(a.question)}</div>
    <div class="bar"><i style="width:100%"></i></div>
    <div class="tid cd"></div>
    ${a.options&&a.options.length?`<div class="opts">${a.options.map(o=>`<button data-a="${esc(o)}" class="${o===a.default?'def':''}">${esc(o)}</button>`).join("")}</div>`:""}
    <div class="row"><input placeholder="Type a reply…"><button style="width:70px" data-send>Send</button></div></div>`).join("")
    +nl.map(a=>`<div class="attc al ${esc(a.kind)}" data-al="${esc(a.id)}">
    <div><span class="kb">${esc(NK_LBL[a.kind]||a.kind)}</span> · <b>${esc(nick(a.agent))}</b> <span class="tid">${esc(tname(a.task_id))}</span></div>
    <div class="q">${esc(a.text)}</div>
    <div class="row"><button class="ghost" data-open="${esc(a.root_id)}">open task</button><button data-dismiss>Dismiss</button></div></div>`).join("");
  for(const c of $("attlist").querySelectorAll(".attc.al")){
    c.querySelector("[data-dismiss]").onclick=()=>ackAlerts({ids:[c.dataset.al]});
    c.querySelector("[data-open]").onclick=()=>selectTask(c.querySelector("[data-open]").dataset.open);
  }
  for(const c of $("attlist").querySelectorAll(".attc:not(.al)")){
    const id=c.dataset.id,inp=c.querySelector("input");
    c.querySelectorAll("[data-a]").forEach(b=>b.onclick=()=>answer(id,b.dataset.a));
    c.querySelector("[data-send]").onclick=()=>inp.value.trim()&&answer(id,inp.value.trim());
    inp.onkeydown=e=>{if(e.key==="Enter"&&inp.value.trim())answer(id,inp.value.trim())};
  }
  tickAtt();attStyle();updateGraph();
  for(const o of $("filter").options){const base=tname(o.value)||"all tasks";const w=o.value&&(list.some(a=>rootOf(a.task_id)===o.value)||nl.some(a=>a.root_id===o.value));o.textContent=(w?"⚠ ":"")+(o.value?base:"all tasks")}
}
let lastTick=Date.now();
function tickAtt(){const now=Date.now(),dt=now-lastTick;lastTick=now;
  for(const c of $("attlist").querySelectorAll(".attc")){const a=state.att[c.dataset.id];if(!a)continue;
    if(state.status[rootOf(a.task_id)]==="paused"){a._deadline+=dt;c.querySelector(".cd").textContent="timer stopped: the task is paused";continue}
    const left=Math.max(0,(a._deadline-Date.now())/1000);c.querySelector(".bar i").style.width=(100*left/a.timeout)+"%";
    c.querySelector(".cd").textContent=left>0?`auto-confirms "${a.default}" in ${Math.ceil(left)}s`:"auto-confirming…"}
}
setInterval(tickAtt,1000);
// system-wide "no limits" hour (backend state in /api/limits; every tab shows the same countdown)
const LIM={until:null};
async function loadLimits(){try{setLimits(await api("/api/limits"))}catch(e){}}
function setLimits(s){LIM.until=s&&s.active?Date.now()+s.remaining*1000:null;paintLimits()}
function paintLimits(){const left=LIM.until?Math.max(0,Math.ceil((LIM.until-Date.now())/1000)):0;
  $("limchk").checked=left>0;$("limbox").classList.toggle("on",left>0);$("limre").hidden=!left;
  $("limleft").textContent=left?`${Math.floor(left/60)}:${String(left%60).padStart(2,"0")}`:"";
  if(LIM.until&&!left){LIM.until=null;loadLimits()}}  // the hour is over: the backend has reverted
async function postLimits(enabled){try{setLimits(await api("/api/limits",{method:"POST",body:JSON.stringify({enabled})}))}
  catch(e){alert("Could not change the limits: "+e.message);loadLimits()}}
$("limchk").onchange=e=>postLimits(e.target.checked);$("limre").onclick=()=>postLimits(true);
setTimeout(loadLimits);setInterval(paintLimits,1000);setInterval(loadLimits,60000);
async function answer(id,text){
  const r=await fetch(`/api/attention/${encodeURIComponent(id)}`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({answer:text})});
  if(r.ok||r.status===409)removeAttention(id);else alert(await r.text());
}
async function loadAttention(){for(const a of await (await fetch("/api/attention?pending=true")).json())addAttention(a,false);
  const live=new Set((await (await fetch("/api/attention?pending=true")).json()).map(a=>a.id));for(const id of Object.keys(state.att))if(!live.has(id))removeAttention(id)}
$("attbadge").onclick=()=>$("attsec").scrollIntoView({behavior:"smooth"});
$("attack").onclick=()=>ackAlerts({});

/* ---------------- browser alerts ---------------- */
let audio=null;const baseTitle=document.title;
function unlockAudio(){if(!audio){try{audio=new (window.AudioContext||window.webkitAudioContext)()}catch{}}}
document.addEventListener("pointerdown",unlockAudio,{once:true});
function beep(){if(!audio||!store.get("sound",true))return;const t=audio.currentTime;
  [[880,0],[1175,.18]].forEach(([f,d])=>{const o=audio.createOscillator(),g=audio.createGain();o.frequency.value=f;o.type="sine";
    g.gain.setValueAtTime(0,t+d);g.gain.linearRampToValueAtTime(.18,t+d+.02);g.gain.exponentialRampToValueAtTime(.001,t+d+.25);o.connect(g).connect(audio.destination);o.start(t+d);o.stop(t+d+.3)})}
function alertUser(a){
  beep();
  if("Notification" in window&&Notification.permission==="granted"){
    try{const n=new Notification(`${nick(a.agent)} needs your attention`,{body:a.question.slice(0,200),tag:a.id,requireInteraction:true});n.onclick=()=>{window.focus();n.close()}}catch{}
  }
}
setInterval(()=>{const n=Object.keys(state.att).length+notices().length;
  if(!n){if(document.title!==baseTitle)document.title=baseTitle;return}
  document.title=document.title.startsWith("⚠")?`(${n}) ${baseTitle}`:`⚠ ${n} need${n===1?"s":""} attention`},1000);
function alertsLabel(){const p="Notification" in window?Notification.permission:"unsupported";
  $("alerts").textContent=p==="granted"?(store.get("sound",true)?"🔔 Alerts on":"🔕 Sound off"):"🔔 Enable alerts"}
$("alerts").onclick=async()=>{unlockAudio();
  if("Notification" in window&&Notification.permission==="default")await Notification.requestPermission();
  else if("Notification" in window&&Notification.permission==="granted")store.set("sound",!store.get("sound",true));
  alertsLabel();beep()};
alertsLabel();

