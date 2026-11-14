/* Notes board, artifacts list and the artifact viewer */
/* ---------------- notes board ---------------- */
const noteRoot=()=>$("filter").value||state.lastRoot;
const boardId=()=>{const r=noteRoot(),sc=$("nscope").value;return r&&sc?`${r}.${sc}`:r};
async function loadNotes(){const r=noteRoot(),bid=boardId();$("notetask").textContent=r?tname(r):"";
  if(NB.shown!==bid){NB.shown=bid;NB.resume()}  // another task or board: start at its newest note
  if(!r){$("notes").innerHTML='<div class="sub">Start a task to use its notes board.</div>';return}
  const d=await (await fetch(`/api/memory/${encodeURIComponent(boardId())}`)).json();
  const box=$("notes"),top=box.scrollTop;
  box.innerHTML=d.notes.length?d.notes.map(n=>`<div class="note"><b>${esc(n.key)}</b>: ${esc(n.value)}<span class="tid">${esc(n.author)} · ${esc(n.ts.slice(11,19))}</span></div>`).join(""):'<div class="sub">No notes yet. Agents add facts and decisions here with the note tool.</div>';
  if(NB.on)NB.stick();else box.scrollTop=NB.last=top}  // scrolled up: keep the reader's place
$("nadd").onclick=async()=>{const r=boardId(),k=$("nkey").value.trim();if(!r||!k)return;
  const res=await fetch(`/api/memory/${encodeURIComponent(r)}`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({key:k,value:$("nval").value})});
  if(res.ok){$("nkey").value="";$("nval").value="";NB.resume();loadNotes()}else alert(await res.text())};

/* ---------------- artifacts ---------------- */
/* One collapsible group per task (department workspaces "<task>.<dept>" nest under their task).
   Open/closed state and the "active task only" switch are kept in localStorage. */
const loadArtifactsSoon=debounce(()=>loadArtifacts(),250);
let artRows=[];
const artOpen=store.get("artOpen",{});  // details key -> open (only what the user toggled)
const artParent=t=>{const i=t.lastIndexOf(".");return i>0?t.slice(0,i):t};
const artActive=()=>$("filter").value||state.lastRoot;
$("artactive").checked=!!store.get("artActive",false);
$("artactive").onchange=()=>{store.set("artActive",$("artactive").checked);loadArtifacts()};
function artAll(open){for(const d of $("artifacts").querySelectorAll("details.tgrp"))artOpen[d.dataset.k]=open;store.set("artOpen",artOpen);loadArtifacts()}
$("artcollapse").onclick=()=>artAll(false);$("artexpand").onclick=()=>artAll(true);
function artGroup(t,recs,isOpen,dept){
  const files={};
  for(const r of recs)if(r.file)(files[r.file]??=[]).push(r);
  const tree=Object.keys(files).sort().map(f=>{const v=files[f],last=v[v.length-1],depth=f.split("/").length-1;
    const name=last.action==="delete"?`<s>${esc(f)}</s>`:`<span class="link" data-root="${esc(t)}" data-file="${esc(f)}">${esc(f)}</span>`;
    return `<div class="art" style="padding-left:${depth*10}px">${name} <span class="tid">${v.length} version${v.length>1?"s":""} · <span class="act ${esc(last.action)}">${esc(last.action)}</span> · ${esc(last.agent)}</span></div>`}).join("");
  const hist=recs.slice().reverse().map(r=>`<div class="art">${r.path?`<span class="link" data-root="${esc(t)}" ${r.file?`data-file="${esc(r.file)}" data-ver="${esc(r.path)}"`:`data-snip="${esc(r.path)}"`}>${esc(r.file||r.path.split("/").pop())}</span>`:esc(r.file)} <span class="tid"><span class="act ${esc(r.action||"snippet")}">${esc(r.action||"snippet")}</span> · ${esc(r.agent)} · ${esc(r.timestamp)}${r.error?` · ${esc(r.error.slice(0,120))}`:""}</span></div>`).join("");
  const k=t+":h",nf=Object.keys(files).length,full=tname(t),cut=full.indexOf(" : ");
  /* line 1: the task's id (a department workspace: its department), line 2: the title, line 3: count and tools */
  const [id,title]=dept?[full.split(" · ").pop()||t,""]:cut>0?[full.slice(0,cut),full.slice(cut+3)]:[t,full===t?"":full];
  return {nf,html:`<details class="tgrp${dept?" dept":""}" data-k="${esc(t)}" data-r="${isOpen?1:0}" ${isOpen?"open":""}><summary class="grp"><span class="tname"><span class="chev">▸</span><span class="tid" data-id title="${esc(t)}">${esc(id)}</span></span>`+
    (title?`<span class="ttitle" data-title title="${esc(title)}">${esc(title)}</span>`:"")+
    `<span class="ttools"><span class="sub" data-count>${nf} file${nf===1?"":"s"}</span><span class="spacer"></span><button class="ghost" data-fold="${esc(t)}" title="Task, workspace and attachment folders: copy the path or open in your editor">📁</button> <button class="ghost" data-git="${esc(t)}" title="Commit history and push">git</button> <a class="ghost" href="/api/artifacts/zip/${encodeURIComponent(t)}?source_only=true" title="Project files only: no node_modules, venv, vendor, target, bin/obj, dist, build or caches">source ⬇</a> <a class="ghost" href="/api/artifacts/zip/${encodeURIComponent(t)}?workspace_only=true">workspace ⬇</a> <a class="ghost" href="/api/artifacts/zip/${encodeURIComponent(t)}">all ⬇</a></span></span></summary><div class="tbody">`+
    (openFolders.has(t)?`<div class="fold" data-fbox="${esc(t)}"><span class="sub">loading…</span></div>`:"")+
    (tree?`<div class="sub">workspace files</div>${tree}`:"")+
    (recs.length?`<details data-k="${esc(k)}" data-r="${artOpen[k]?1:0}" ${artOpen[k]?"open":""}><summary class="sub" style="cursor:pointer">history · ${recs.length} artifacts</summary>${hist}</details>`:"")}
}
async function loadArtifacts(){
  artRows=await (await fetch("/api/artifacts")).json();const by={};
  for(const r of artRows){if(state.archived.has(r.root_task_id||r.task_id)||state.archived.has(artParent(r.root_task_id||r.task_id)))continue;  // archived tasks: hidden here too
    (by[r.root_task_id||r.task_id]??=[]).push(r);if(r.root_task_name)state.names[r.root_task_id]??=r.root_task_name}
  const tops=[],kids={};  // top-level tasks newest first; department workspaces under their task
  for(const t of Object.keys(by).reverse()){const p=artParent(t);if(!tops.includes(p))tops.push(p);if(p!==t)(kids[p]??=[]).push(t)}
  const act=artActive(),only=$("artactive").checked&&act,shown=only?tops.filter(t=>t===artParent(act)):tops;
  const isOpen=(k,def)=>k in artOpen?artOpen[k]:def;
  $("artifacts").innerHTML=shown.map(t=>{
    const sub=(kids[t]||[]).map(d=>artGroup(d,by[d],isOpen(d,true),true).html+"</div></details>").join("");
    const g=artGroup(t,by[t]||[],isOpen(t,t===act||t===tops[0]||!!only),false);
    return g.html+sub+"</div></details>"}).join("")||
    `<div class="sub">${only?`No artifacts for ${esc(tname(act))} yet.`:"No artifacts yet."}</div>`;
  const keep=(d,open)=>{if(open===(d.dataset.r==="1"))return;d.dataset.r=open?"1":"0";artOpen[d.dataset.k]=open;store.set("artOpen",artOpen)};
  for(const d of $("artifacts").querySelectorAll("details[data-k]")){  // remember only what the user changed
    d.ontoggle=()=>keep(d,d.open);  // keyboard and anything else
    d.querySelector(":scope>summary").onclick=ev=>{if(!ev.target.closest("a,button"))keep(d,!d.open)}}  // at once, so a re-render right after keeps it
  for(const b of $("artifacts").querySelectorAll("[data-git]"))b.onclick=ev=>{ev.preventDefault();openGit(b.dataset.git)};
  for(const b of $("artifacts").querySelectorAll("[data-fold]"))b.onclick=ev=>{ev.preventDefault();const t=b.dataset.fold;openFolders.has(t)?openFolders.delete(t):openFolders.add(t);
    artOpen[t]=true;store.set("artOpen",artOpen);loadArtifacts()};  // showing folders opens the group
  for(const el of $("artifacts").querySelectorAll("[data-fbox]"))drawFolders(el);
  for(const l of $("artifacts").querySelectorAll(".link"))l.onclick=()=>l.dataset.snip?openSnippet(l.dataset.root,l.dataset.snip):openFile(l.dataset.root,l.dataset.file,l.dataset.ver);
}

const openFolders=new Set();
async function drawFolders(el){
  const r=await fetch(`/api/folders/${encodeURIComponent(el.dataset.fbox)}`);
  if(!r.ok){el.innerHTML=`<span class="sub">${esc((await r.json().catch(()=>({}))).detail||"no folders")}</span>`;return}
  const d=await r.json();
  el.innerHTML=d.folders.map(f=>`<div class="frow"><span class="k">${esc(f.kind)}</span><code title="${esc(f.path)}" style="${f.exists?"":"opacity:.5"}"><bdi>${esc(f.path)}</bdi></code>`+
    `<button class="ghost" data-copy="${esc(f.path)}" title="Copy path">copy</button>`+
    (f.uri&&f.exists?`<a class="ghost" href="${esc(f.uri)}" title="${d.remote?"Open over SSH in your editor":"Open in your editor"}">open ↗</a>`:"")+`</div>`).join("")+
    (d.hint?`<div class="sub">${esc(d.hint)}</div>`:"");
  for(const b of el.querySelectorAll("[data-copy]"))b.onclick=()=>copyText(b.dataset.copy).then(ok=>toast(ok?"Path copied":"Copy failed: select the path and copy it"));
}
async function copyText(text){
  try{await navigator.clipboard.writeText(text);return true}catch{}
  const ta=document.createElement("textarea");ta.value=text;ta.style.position="fixed";ta.style.opacity="0";document.body.appendChild(ta);ta.select();
  let ok=false;try{ok=document.execCommand("copy")}catch{}ta.remove();return ok;
}

/* ---------------- artifact viewer ---------------- */
const V={root:null,file:null,vers:[],sel:null,cmp:null,mode:"view",cache:{},rmode:store.get("rmode","rendered")};
const renderable=(name,lang)=>/\.(md|markdown|mdx|mmd|mermaid)$/i.test(name||"")||["markdown","md","mermaid"].includes((lang||"").toLowerCase());
const isMermaid=(name,lang)=>/\.(mmd|mermaid)$/i.test(name||"")||(lang||"").toLowerCase()==="mermaid";
async function fetchText(path){if(!(path in V.cache))V.cache[path]=await (await fetch(`/api/artifacts/file/${enc(path)}`)).text();return V.cache[path]}
function langOf(name,lang){const ext=(name.split(".").pop()||"").toLowerCase();const map={py:"python",js:"javascript",ts:"typescript",md:"markdown",sh:"bash",yml:"yaml",rs:"rust",rb:"ruby",cs:"csharp",kt:"kotlin"};
  const l=(lang&&lang!=="text")?lang:(map[ext]||ext);return window.hljs&&hljs.getLanguage(l)?l:null}
function numbered(text,html){const n=Math.max(1,text.split("\n").length-(text.endsWith("\n")?1:0));
  return `<div class="src"><pre class="ln" aria-hidden="true">${Array.from({length:n},(_,i)=>i+1).join("\n")}</pre><pre><code class="hljs">${html}</code></pre></div>`}
function highlighted(text,name,lang){const l=langOf(name,lang);
  if(window.hljs&&l){try{return hljs.highlight(text,{language:l,ignoreIllegals:true}).value}catch{}}
  return esc(text)}
async function openFile(root,file,ver){
  V.root=root;V.file=file;V.vers=(await (await fetch(`/api/workspace/${encodeURIComponent(root)}/history/${enc(file)}`)).json()).filter(r=>r.path);
  if(!V.vers.length)return;
  const i=ver?V.vers.findIndex(r=>r.path===ver):V.vers.length-1;V.sel=Math.max(0,i);V.cmp=V.sel>0?V.sel-1:null;
  V.mode=ver&&V.vers[V.sel].action==="patch"&&V.cmp!==null?"diff":"view";
  $("mfile").textContent=file;$("mtask").textContent=tname(root);$("modal").style.display="block";$("vers").style.display="";
  $("mdiff").style.display=V.vers.length>1?"":"none";drawViewer();
}
async function openSnippet(root,path){
  V.root=root;V.file=null;V.vers=[{path,action:"snippet",timestamp:path.split("/").pop(),agent:path.split("/")[1],language:""}];V.sel=0;V.cmp=null;V.mode="view";
  $("mfile").textContent=path.split("/").pop();$("mtask").textContent=tname(root);$("modal").style.display="block";
  $("vers").style.display="none";$("mdiff").style.display="none";drawViewer();
}
async function drawViewer(){
  const v=V.vers[V.sel];
  $("vers").innerHTML=V.vers.map((r,i)=>`<div class="ver ${i===V.sel?'sel':''} ${i===V.cmp&&V.mode==='diff'?'cmp':''}" data-i="${i}">
    <b>v${i+1}</b> <span class="act ${esc(r.action)}">${esc(r.action)}</span> · ${esc(r.agent)}<div class="tid">${esc(r.timestamp)}${r.commit?` · <code>${esc(r.commit.slice(0,8))}</code>`:""}</div></div>`).reverse().join("")+
    (V.vers.length>1?'<div class="sub" style="padding:6px">Click a version to view it. Shift+click picks the version to compare against.</div>':"");
  for(const el of $("vers").querySelectorAll(".ver"))el.onclick=ev=>{const i=+el.dataset.i;if(ev.shiftKey&&i!==V.sel){V.cmp=i;V.mode="diff"}else{V.sel=i;if(V.cmp===i||V.cmp===null)V.cmp=i>0?i-1:null}drawViewer()};
  $("mdl").href=`/api/artifacts/file/${enc(v.path)}`;
  $("mroll").style.display=V.file&&V.sel!==V.vers.length-1?"":"none";
  $("mview").classList.toggle("sel",V.mode==="view");
  const name=V.file||v.path;
  if(v.language==="binary"||(V.mode==="diff"&&V.cmp!==null&&V.vers[V.cmp].language==="binary")){
    /* binary files (e.g. received from a peer): an image preview, otherwise size and the download link */
    $("mtabs").classList.remove("on");const url=`/api/artifacts/file/${enc(v.path)}`;
    $("viewer").innerHTML=/\.(png|jpe?g|gif|webp)$/i.test(name)&&v.language==="binary"?`<div class="binv"><img src="${url}" alt="${esc(name)}"><div class="tid">${size(v.bytes||0)} · binary</div></div>`
      :`<div class="binv"><p>Binary file${v.bytes?` (${size(v.bytes)})`:""}: no text preview. Use <b>download</b> above.</p></div>`;return}
  if(V.mode==="diff"&&V.cmp!==null){
    $("mtabs").classList.remove("on");
    const [a,b]=await Promise.all([fetchText(V.vers[V.cmp].path),fetchText(v.path)]);
    $("viewer").innerHTML=sideBySide(a,b,`v${V.cmp+1} · ${V.vers[V.cmp].timestamp}`,`v${V.sel+1} · ${v.timestamp}`);
    for(const f of $("viewer").querySelectorAll("tr.fold"))f.onclick=()=>{f.nextElementSibling&&f.parentElement.querySelectorAll(`tr[data-f="${f.dataset.f}"]`).forEach(r=>r.style.display="");f.remove()};
  }else{
    const text=await fetchText(v.path),src=numbered(text,highlighted(text,name,v.language));
    const rich=renderable(name,v.language)&&window.MD&&MD.ready();
    $("mtabs").classList.toggle("on",!!rich);
    for(const b of $("mtabs").querySelectorAll("button"))b.classList.toggle("sel",b.dataset.mt===V.rmode);
    if(!rich||V.rmode==="source"){$("viewer").innerHTML=src;return}
    const doc=document.createElement("div");doc.className="md";
    doc.innerHTML=MD.render(isMermaid(name,v.language)?"```mermaid\n"+text+"\n```":text);
    const wrap=document.createElement("div");wrap.className="mdoc";const t=MD.toc(doc);
    if(t)wrap.insertAdjacentHTML("afterbegin",t);wrap.appendChild(doc);
    if(V.rmode==="split"){$("viewer").innerHTML=`<div class="split"><div>${src}</div><div></div></div>`;$("viewer").querySelector(".split>div:last-child").appendChild(wrap)}
    else{$("viewer").innerHTML="";$("viewer").appendChild(wrap)}
    for(const a of wrap.querySelectorAll("[data-toc]"))a.onclick=ev=>{ev.preventDefault();const h=doc.querySelector("#"+CSS.escape(a.dataset.toc));if(h)h.scrollIntoView({behavior:"smooth",block:"start"})};
    MD.enhance(wrap);
  }
}
function lineDiff(a,b){
  const A=a.split("\n"),B=b.split("\n");let s=0;while(s<A.length&&s<B.length&&A[s]===B[s])s++;
  let ea=A.length,eb=B.length;while(ea>s&&eb>s&&A[ea-1]===B[eb-1]){ea--;eb--}
  const a2=A.slice(s,ea),b2=B.slice(s,eb),n=a2.length,m=b2.length,ops=[];
  for(let i=0;i<s;i++)ops.push(["=",i,i]);
  if(n*m>4e6){a2.forEach((_,i)=>ops.push(["-",s+i,null]));b2.forEach((_,j)=>ops.push(["+",null,s+j]))}
  else{const L=Array.from({length:n+1},()=>new Uint32Array(m+1));
    for(let i=n-1;i>=0;i--)for(let j=m-1;j>=0;j--)L[i][j]=a2[i]===b2[j]?L[i+1][j+1]+1:Math.max(L[i+1][j],L[i][j+1]);
    let i=0,j=0;while(i<n&&j<m){if(a2[i]===b2[j]){ops.push(["=",s+i,s+j]);i++;j++}else if(L[i+1][j]>=L[i][j+1]){ops.push(["-",s+i,null]);i++}else{ops.push(["+",null,s+j]);j++}}
    while(i<n)ops.push(["-",s+i++,null]);while(j<m)ops.push(["+",null,s+j++])}
  for(let i=ea,j=eb;i<A.length;i++,j++)ops.push(["=",i,j]);
  return {A,B,ops};
}
function sideBySide(a,b,la,lb){
  const {A,B,ops}=lineDiff(a,b),rows=[];let k=0;
  while(k<ops.length){const o=ops[k];
    if(o[0]==="="){rows.push(["=",o[1],o[2]]);k++;continue}
    const dels=[],adds=[];while(k<ops.length&&ops[k][0]!=="="){(ops[k][0]==="-"?dels:adds).push(ops[k]);k++}
    for(let i=0;i<Math.max(dels.length,adds.length);i++)rows.push(["~",dels[i]?dels[i][1]:null,adds[i]?adds[i][2]:null])}
  const changed=rows.map(r=>r[0]!=="="),near=rows.map((_,i)=>changed.slice(Math.max(0,i-3),i+4).some(Boolean));
  let html=`<table class="diff"><tr><td class="n"></td><td class="tid">${esc(la)}</td><td class="n"></td><td class="tid">${esc(lb)}</td></tr>`,fold=0;
  const cell=(n,t,cls)=>n===null?`<td class="n"></td><td class="emp"></td>`:`<td class="n">${n+1}</td><td class="${cls}">${esc(t)}</td>`;
  for(let i=0;i<rows.length;i++){const r=rows[i];
    if(!near[i]){let j=i;while(j<rows.length&&!near[j])j++;fold++;
      html+=`<tr class="fold" data-f="${fold}"><td colspan="4">⋯ ${j-i} unchanged lines (click to show)</td></tr>`;
      for(let q=i;q<j;q++)html+=`<tr data-f="${fold}" style="display:none">${cell(rows[q][1],A[rows[q][1]],"")}${cell(rows[q][2],B[rows[q][2]],"")}</tr>`;
      i=j-1;continue}
    html+=r[0]==="="?`<tr>${cell(r[1],A[r[1]],"")}${cell(r[2],B[r[2]],"")}</tr>`:`<tr>${cell(r[1],r[1]===null?"":A[r[1]],"del")}${cell(r[2],r[2]===null?"":B[r[2]],"add")}</tr>`}
  return html+(rows.every(r=>r[0]==="=")?`<tr><td colspan="4" class="tid" style="padding:10px">No differences.</td></tr>`:"")+"</table>";
}
$("mview").onclick=()=>{V.mode="view";drawViewer()};
for(const b of $("mtabs").querySelectorAll("button"))b.onclick=()=>{V.rmode=b.dataset.mt;store.set("rmode",V.rmode);V.mode="view";drawViewer()};
$("mdiff").onclick=()=>{if(V.cmp===null)V.cmp=V.sel>0?V.sel-1:(V.vers.length>1?1:null);if(V.cmp!==null){V.mode="diff";drawViewer()}};
$("mclose").onclick=()=>{$("modal").style.display="none"};
document.addEventListener("keydown",e=>{if(e.key==="Escape")$("modal").style.display="none"});
$("mroll").onclick=async()=>{const v=V.vers[V.sel];if(!confirm(`Roll ${V.file} back to v${V.sel+1} (${v.timestamp})? This saves a new version.`))return;
  const r=await fetch(`/api/workspace/${encodeURIComponent(V.root)}/rollback`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({file:V.file,version:v.path})});
  if(!r.ok){alert(await r.text());return}await loadArtifacts();openFile(V.root,V.file)};

