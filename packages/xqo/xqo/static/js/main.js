/* Plugins, attachments, wiring and start-up (loaded last) */
/* ---------------- plugins: each active plugin brings its own nav link and UI script (/api/plugins: nav, ui) ---------------- */
async function loadPlugins(){let r;try{r=await api("/api/plugins")}catch(err){return}
  XQO.plugins=Object.fromEntries(r.items.map(p=>[p.name,p]));
  for(const p of r.items.filter(p=>p.state==="active")){
    if(p.nav&&p.nav.href)XQO.button({id:p.nav.id,label:p.nav.label,href:p.nav.href,title:p.nav.title,icon:p.nav.icon});
    await Promise.all((p.ui||[]).map(src=>XQO.load(src,p.name)))}
  const bad=r.items.filter(p=>p.state==="failed");
  if(bad.length)toast(`⚠ plugin${bad.length>1?"s":""} off after an error: ${bad.map(p=>`${p.name} (${p.error})`).join("; ")}`,null)}
/* ---------------- attachments ---------------- */
const ATT={items:[],n:0},ALIMIT={image:5e6,pdf:20e6,text:2e6},PASTE_AS_FILE=2000;
const size=n=>n>=1e6?(n/1e6).toFixed(1)+" MB":Math.max(1,Math.round(n/1e3))+" KB";
const stamp=()=>new Date().toISOString().replace(/\D/g,"").slice(2,14);
const kindOf=f=>/^image\//.test(f.type)?"image":(f.type==="application/pdf"||/\.pdf$/i.test(f.name))?"pdf":"text";
const attUrl=(root,n)=>`/api/attachments/${encodeURIComponent(root)}/${encodeURIComponent(n)}`;
function attLinks(root,list){if(!list||!list.length||!root)return "";
  return `<div class="atts">${list.map(a=>`<a class="achip" href="${attUrl(root,a.name)}" target="_blank" title="${esc(a.name)} · ${esc(a.kind)} · ${size(a.bytes)}">${a.kind==="image"?`<img src="${attUrl(root,a.name)}" alt="" loading="lazy">`:`<span>${a.kind==="pdf"?"📄":"📝"}</span>`}<span class="nm">${esc(a.name)}</span></a>`).join("")}</div>`}
function addFiles(list,text=null){
  for(const f of list){const kind=kindOf(f);
    if(f.size>ALIMIT[kind]){alert(`${f.name}: ${kind} files are limited to ${ALIMIT[kind]/1e6} MB`);continue}
    if(!f.size){alert(`${f.name} is empty`);continue}
    ATT.items.push({id:++ATT.n,file:f,name:f.name,kind,url:kind==="image"?URL.createObjectURL(f):null,text})}
  drawAtts()}
function removeAtt(id){const a=ATT.items.find(x=>x.id===id);if(a&&a.url)URL.revokeObjectURL(a.url);ATT.items=ATT.items.filter(x=>x.id!==id);drawAtts()}
function drawAtts(){
  $("atts").innerHTML=ATT.items.map(a=>`<span class="achip" data-id="${a.id}">${a.url?`<img src="${a.url}" alt="">`:`<span>${a.kind==="pdf"?"📄":"📝"}</span>`}<span class="nm" title="${esc(a.name)}">${esc(a.name)}</span><span class="tid">${size(a.file.size)}</span>${a.text!==null?'<button class="ghost" data-inline title="Put the pasted text in the prompt instead">inline</button>':""}<button class="ghost" data-x title="Remove">✕</button></span>`).join("");
  for(const c of $("atts").querySelectorAll(".achip")){const id=+c.dataset.id;
    c.querySelector("[data-x]").onclick=()=>removeAtt(id);
    const il=c.querySelector("[data-inline]");if(il)il.onclick=()=>{const a=ATT.items.find(x=>x.id===id),p=$("prompt");
      p.setRangeText(a.text,p.selectionStart,p.selectionEnd,"end");removeAtt(id);p.focus()}}
}
$("attach").onclick=()=>$("afile").click();
$("afile").onchange=()=>{addFiles([...$("afile").files]);$("afile").value=""};
$("prompt").addEventListener("paste",e=>{const cd=e.clipboardData;if(!cd)return;
  const files=[...cd.files];
  if(files.length){e.preventDefault();  // screenshots arrive as "image.png": give them a unique name
    addFiles(files.map((f,i)=>!f.name||f.name==="image.png"?new File([f],`pasted-${stamp()}${files.length>1?"-"+(i+1):""}.${(f.type.split("/")[1]||"bin").replace("jpeg","jpg")}`,{type:f.type}):f));return}
  const t=cd.getData("text/plain");
  if(t.length>PASTE_AS_FILE){e.preventDefault();addFiles([new File([t],`pasted-${stamp()}.txt`,{type:"text/plain"})],t)}});
for(const ev of ["dragenter","dragover"])$("prompt").addEventListener(ev,e=>{if([...e.dataTransfer.types].includes("Files")){e.preventDefault();$("prompt").classList.add("drop")}});
for(const ev of ["dragleave","drop"])$("prompt").addEventListener(ev,()=>$("prompt").classList.remove("drop"));
$("prompt").addEventListener("drop",e=>{if(e.dataTransfer.files.length){e.preventDefault();addFiles([...e.dataTransfer.files])}});
const readAtts=()=>Promise.all(ATT.items.map(async a=>({name:a.name,media_type:a.file.type||null,data:await b64(a.file)})));
const clearAtts=()=>{for(const a of [...ATT.items])removeAtt(a.id)};
const b64=f=>new Promise((ok,err)=>{const r=new FileReader();r.onload=()=>ok(String(r.result).split(",")[1]||"");r.onerror=()=>err(r.error);r.readAsDataURL(f)});

/* ---------------- wiring ---------------- */
$("send").onclick=async()=>{const p=$("prompt").value.trim();if(!p&&!ATT.items.length)return;
  $("send").disabled=true;const pend=setPending(MODE,MODE==="task"?null:$("filter").value);
  if(MODE!=="task"){try{await sendChat(p);$("prompt").value="";$("urgent").checked=false;setMode(MODE)}catch(err){clearPending(pend);alert(err.message)}finally{$("send").disabled=false}return}
  try{const sel=$("filter").value,urgent=$("urgent").checked&&ACTIVE.has(state.status[sel]);
    const attachments=await readAtts();
    const r=await fetch("/api/tasks",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({prompt:p,agent:$("entry").value,attachments,urgent,pause_task:urgent?sel:null})});
    if(r.ok){const d=await r.json();pend.root=d.id;$("prompt").value="";clearAtts();if(urgent){state.status[sel]="paused";$("urgent").checked=false}
      selectTask(d.id,d.name)}
    else{clearPending(pend);const d=await r.json().catch(()=>null);alert(d&&d.detail?(typeof d.detail==="string"?d.detail:JSON.stringify(d.detail)):r.statusText)}}
  catch(err){clearPending(pend);alert("Could not read an attachment: "+err)}
  finally{$("send").disabled=false}};
$("filter").onchange=()=>{if(R.on)stopReplay();rerender();loadTask();loadNotes();loadAgentsSoon();setMode(MODE);loadArtifactsSoon()};
/* after submitting a task: show the new task in the Live viewer, across all departments */
function selectTask(id,name){if(!id)return;state.names[id]=name||state.names[id];addTask(id,name);$("filter").value=id;$("deptf").value="";$("filter").onchange()}
$("deptf").onchange=rerender;$("nscope").onchange=()=>loadNotes();$("hidellm").onchange=rerender;$("refresh").onclick=()=>loadArtifacts();
$("prompt").addEventListener("keydown",e=>{if(e.key==="Enter"&&(e.ctrlKey||e.metaKey)){e.preventDefault();$("send").click()}});
(async()=>{
  initResizers();initColumns();initAltLayout();setMode("task");
  loadPlugins();
  await loadAgents();
  try{for(const e of (await api("/api/archive")).items)state.archived.add(e.root_id)}catch{}  // hidden until unarchived
  for(const e of await (await fetch("/api/history")).json())onEvent(e,false);
  await Promise.all([loadArtifacts(),loadAttention(),loadNotes(),loadAlerts(),loadUsage()]);
  setInterval(loadUsage,60000);  // plan limit windows that reset drop off the chip
  api("/api/trash").then(t=>state.retention=t.retention_days).catch(()=>{});
  const es=new EventSource("/api/events");
  es.onopen=async()=>{$("dot").classList.add("on");$("status").textContent="live";
    for(const e of await (await fetch("/api/history")).json())onEvent(e,false);loadAttention();loadAlerts();loadUsage()};  // backfill after reconnect
  es.onerror=()=>{$("dot").classList.remove("on");$("status").textContent="reconnecting…";if(window.xqoAuthCheck)window.xqoAuthCheck().catch(()=>{})};  // signed out: to /login
  es.onmessage=m=>onEvent(JSON.parse(m.data),true);
  const qt=new URLSearchParams(location.search).get("task");  // links from the office view: /?task=<id>
  if(qt){history.replaceState(null,"",location.pathname);selectTask(qt)}
})();
