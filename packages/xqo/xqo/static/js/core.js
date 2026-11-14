/* xqo dashboard: shared state and helpers, the plugin UI API (XQO), side columns, layout, resizers.
   The dashboard is plain scripts that share one global scope, loaded in order by index.html; main.js starts it. */
const KINDS=["task","ack","direct","reply","broadcast","offer","handover","result","artifact","memory","sandbox","mcp","fork","git","departments","peer","channel","feed","attention_requested","attention_resolved","llm","error","done"];
const TOMB=new Set(["task_deleted","task_restored","task_archived","task_unarchived"]);  // records that outlive a hidden task
const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const enc=p=>p.split('/').map(encodeURIComponent).join('/');
const state={openNodes:new Set(),agents:[],events:[],tasks:new Set(),seen:new Set(),pos:{},names:{},att:{},activity:{},lastRoot:null,status:{},
  archived:new Set(),sched:new Set()};  // archived task trees (hidden); tasks a schedule started (⏱ in the task list)
const R={on:false,root:null,ev:[],pos:-1,timer:null};  // replay state
const tname=id=>state.names[id]||id;
const fmt=n=>n>=1e6?(n/1e6).toFixed(1)+"M":n>=1e3?(n/1e3).toFixed(1)+"k":String(n);
const debounce=(fn,ms)=>{let t;return(...a)=>{clearTimeout(t);t=setTimeout(()=>fn(...a),ms)}};
const store={get(k,d){try{const v=localStorage.getItem(k);return v===null?d:JSON.parse(v)}catch{return d}},set(k,v){try{localStorage.setItem(k,JSON.stringify(v))}catch{}}};
state.openDepts=new Set();  // departments expanded in the Departments & agents panel; all start collapsed on page load
$("legend").innerHTML=KINDS.filter(k=>!k.startsWith("attention_")).concat(["attention_requested"]).map(k=>`<span style="--k:var(--${k})">● ${k.replace("_requested","")}</span>`).join("");

/* ---------------- hideable side columns ---------------- */
function initColumns(){
  const narrow=matchMedia("(max-width:900px)").matches;
  const set=(side,hide)=>{document.body.classList.toggle("hide"+side,hide);const b=$("tog"+side);b.classList.toggle("off",hide);
    b.title=`${hide?"Show":"Hide"} the ${side==="L"?"left":"right"} column`;b.setAttribute("aria-pressed",String(!hide))};
  for(const side of ["L","R"]){
    set(side,store.get("hide"+side,narrow));  // narrow windows start with both drawers closed
    $("tog"+side).onclick=()=>{const hide=!document.body.classList.contains("hide"+side);set(side,hide);store.set("hide"+side,hide);
      if(!hide&&matchMedia("(max-width:900px)").matches)set(side==="L"?"R":"L",true);  // one drawer at a time
      setTimeout(()=>window.dispatchEvent(new Event("resize")),0)}}
}

/* ---------------- alternative layout ---------------- */
/* Off: notes board under the agents (left), message flow under the live viewer, flow graph top right.
   On: flow graph takes the message flow's slot (which is hidden), notes board takes the graph's slot. */
function setAltLayout(on){
  document.body.classList.toggle("alt",on);const b=$("togalt");b.classList.toggle("off",!on);b.setAttribute("aria-pressed",String(on));
  b.title=on?"Back to the default layout":"Alternative layout: flow graph in the middle, notes board on the right, message flow hidden";
  const g=$("graphsec"),n=$("notesec"),r1=document.querySelector('.vres[data-key="R1"]'),h=(store.get("h:R1",null)||300)+"px";
  if(on){$("colM").insertBefore(g,$("flowsec"));$("colR").insertBefore(n,r1);
    g.className="grow";g.style.height=g.style.flex="";n.className="fixed";n.style.height=h;n.style.flex="none"}
  else{$("colR").insertBefore(g,r1);$("colL").appendChild(n);
    n.className="grow";n.style.height=n.style.flex="";g.className="fixed";g.style.height=h;g.style.flex="none"}
  setTimeout(()=>window.dispatchEvent(new Event("resize")),0);
}
function initAltLayout(){
  if(store.get("altLayout",false))setAltLayout(true);else $("togalt").classList.add("off");
  $("togalt").onclick=()=>{const on=!document.body.classList.contains("alt");if(on&&R.on)return;setAltLayout(on);store.set("altLayout",on)};
}

/* ---------------- vertical resizers ---------------- */
function initResizers(){
  const defH={};for(const s of document.querySelectorAll("section"))defH[s.id]=s.style.height;  // inline default heights, for the double-click reset
  for(const h of document.querySelectorAll(".vres")){
    const key="h:"+h.dataset.key,saved=store.get(key,null);let prev=h.previousElementSibling;
    if(saved){prev.style.flex="none";prev.style.height=saved+"px"}
    h.addEventListener("pointerdown",e=>{
      if(e.button!==0||h.classList.contains("drag"))return;
      e.preventDefault();prev=h.previousElementSibling;  // the alternative layout moves panels between slots
      try{h.setPointerCapture(e.pointerId)}catch{}
      h.classList.add("drag");document.body.classList.add("resizing");
      const col=h.parentElement,y0=e.clientY,h0=prev.getBoundingClientRect().height,st0=col.scrollTop;
      // room for the panel: the column minus the handles, fixed panels and the other panels at their minimum height
      let rest=0;for(const c of col.children){if(c===prev)continue;const cs=getComputedStyle(c);if(cs.display==="none")continue;
        rest+=c.classList.contains("vres")||cs.flexGrow==="0"?c.getBoundingClientRect().height:Math.max(60,parseFloat(cs.minHeight)||0)}
      const max=Math.max(60,col.clientHeight-rest,h0);
      let y=y0,raf=0;
      const apply=()=>{raf=0;prev.style.flex="none";prev.style.height=Math.max(60,Math.min(max,h0+y-y0))+"px";col.scrollTop=st0};
      const move=ev=>{if(ev.pointerId!==e.pointerId)return;y=ev.clientY;if(!raf)raf=requestAnimationFrame(apply)};
      const offs=[[window,"pointermove",move],[window,"pointerup",end],[window,"pointercancel",end],[h,"lostpointercapture",end],[window,"blur",end]];
      function end(ev){if(ev&&ev.pointerId!==undefined&&ev.pointerId!==e.pointerId)return;
        for(const [t,n,f] of offs)t.removeEventListener(n,f);
        if(raf){cancelAnimationFrame(raf);apply()}
        h.classList.remove("drag");document.body.classList.remove("resizing");
        try{h.releasePointerCapture(e.pointerId)}catch{}
        store.set(key,Math.round(prev.getBoundingClientRect().height))}
      for(const [t,n,f] of offs)t.addEventListener(n,f);
    });
    h.addEventListener("dblclick",()=>{prev=h.previousElementSibling;prev.style.height=prev.classList.contains("fixed")?(defH[prev.id]||"300px"):"";prev.style.flex="";store.set(key,null)});
  }
}

/* Plugin UI API. A plugin lists browser scripts in its `ui` (served from its `static` folder); each script runs after
   this file and uses: XQO.on(fn) for every live event, XQO.button({...}) for a nav button or link, XQO.plugins
   (the /api/plugins rows by name), and the dashboard's globals ($, esc, api, dialog, toast, debounce, state, ...). */
const XQO={hooks:{event:[]},plugins:{},
  on(fn){this.hooks.event.push(fn)},
  button({id,label,title="",href=null,place="header",onclick=null,hidden=false}){
    if(document.getElementById(id))return document.getElementById(id);
    const el=document.createElement(href?"a":"button");el.className="ghost";el.id=id;el.title=title||"";el.innerHTML=label;
    if(href){el.href=href;el.target="_blank"}if(onclick)el.onclick=onclick;if(hidden)el.style.display="none";
    const where=place==="tasks"?document.getElementById("pluginbtns"):document.getElementById("pluginnav");where.append(el);return el},
  load(src,name){return new Promise(ok=>{const s=document.createElement("script");s.src=src;s.dataset.plugin=name||"";
    s.onload=()=>ok(true);s.onerror=()=>{console.error("plugin UI did not load:",src);ok(false)};document.body.append(s)})}};
