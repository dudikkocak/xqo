/* Flow graph and the user's pending-submission pulse */
/* ---------------- graph ---------------- */
const SVGNS="http://www.w3.org/2000/svg";
const hue=n=>{let h=0;for(const c of n)h=(h*31+c.charCodeAt(0))%360;return h};
function layout(){
  /* one cluster per department (headquarters first); members on a small circle, head on top */
  const dp=(state.depts&&state.depts.length?state.depts:[{name:"hq",head:null,agents:state.agents}]).filter(d=>d.agents.length);
  state.pos={};const boxes=[],ex=state.agents.some(a=>a.avatar)||state.userAvatar?22:0;  // room for labels under avatars
  const mode=graphLayout();
  if(dp.length===1||mode!=="departments"){  // no departments, or a flat layout across them
    const L=arrange(["user",...dp.flatMap(d=>d.agents.map(a=>a.name))],mode==="departments"?"ring":mode,480,320,ex);
    state.pos=L.pos;return {boxes,vb:L.vb}}
  const k=dp.length,cx=260,cy=200,rx=60+24*k,ry=50+19*k;
  dp.forEach((d,i)=>{const t=-Math.PI/2+i*2*Math.PI/k,x=cx+rx*Math.cos(t),y=cy+ry*Math.sin(t);
    const m=[...d.agents].sort((a,b)=>(b.head?1:0)-(a.head?1:0)),n=m.length,rc=n<2?0:16+9*n;
    m.forEach((a,j)=>{const u=-Math.PI/2+j*2*Math.PI/n;state.pos[a.name]=[x+rc*Math.cos(u),y+rc*Math.sin(u)]});
    const pad=28;boxes.push({d,x:x-rc-pad,y:y-rc-pad-10,w:2*(rc+pad),h:2*(rc+pad)+10+ex})});
  const hq=boxes.find(b=>b.d.name==="hq")||boxes[0];state.pos.user=[hq.x+hq.w/2,hq.y-34-(state.userAvatar?22:0)];
  const xs=boxes.flatMap(b=>[b.x,b.x+b.w]),ys=boxes.flatMap(b=>[b.y,b.y+b.h]).concat([state.pos.user[1]-26]);
  const x0=Math.min(...xs)-8,y0=Math.min(...ys)-8;
  return {boxes,vb:[x0,y0,Math.max(...xs)+8-x0,Math.max(...ys)+8-y0]};
}
function graphEdges(){
  const dp=state.depts&&state.depts.length>1?state.depts:null,out=[];
  if(!dp){const names=Object.keys(state.pos);for(let i=0;i<names.length;i++)for(let j=i+1;j<names.length;j++)out.push([names[i],names[j],false]);return out}
  const by=Object.fromEntries(dp.map(d=>[d.name,d])),heads=dp.filter(d=>d.head).map(d=>d.head),hq=(by.hq||{agents:[]}).agents.map(a=>a.name);
  for(const d of dp){const m=d.agents.map(a=>a.name);for(let i=0;i<m.length;i++)for(let j=i+1;j<m.length;j++)out.push([m[i],m[j],false])}
  for(const h of hq){out.push(["user",h,false]);for(const x of heads)out.push([h,x,true])}
  for(let i=0;i<dp.length;i++)for(let j=i+1;j<dp.length;j++){const a=dp[i],b=dp[j];if(!a.head||!b.head)continue;
    const ok=x=>x.contacts==="*"||(x.contacts||[]).includes(a===x?b.name:a.name);if(ok(a)||ok(b))out.push([a.head,b.head,true])}
  return out;
}
function buildGraph(){
  const L=layout(),names=Object.keys(state.pos);$("graph").setAttribute("viewBox",L.vb.map(v=>Math.round(v)).join(" "));
  let defs=`<defs><filter id="glow" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="3" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
    <radialGradient id="gAtt"><stop offset="0" stop-color="#ff5d6c" stop-opacity=".7"/><stop offset="1" stop-color="#ff5d6c" stop-opacity="0"/></radialGradient>`;
  for(const n of names)defs+=`<radialGradient id="g_${esc(n)}"><stop offset="0" stop-color="hsl(${hue(n)},85%,65%)" stop-opacity=".6"/><stop offset="1" stop-color="hsl(${hue(n)},85%,65%)" stop-opacity="0"/></radialGradient>`;
  for(const n of names)defs+=avDef(n);
  defs+=`</defs>`;
  const edges=graphEdges().map(([a,b,x])=>`<path class="edge${x?" xd":""}" d="${curve(a,b)}"/>`).join("");
  const boxes=L.boxes.map(b=>{const c=dcolor(b.d.name);return `<g><rect class="dbox" x="${b.x}" y="${b.y}" width="${b.w}" height="${b.h}" rx="12" fill="${c}" fill-opacity=".05" stroke="${c}" stroke-opacity=".35"/><text class="dlabel" x="${b.x+10}" y="${b.y+14}" fill="${c}">${esc(b.d.name==="hq"?"headquarters":b.d.name)}</text></g>`}).join("");
  const small=L.boxes.length>1;
  const nodes=names.map(n=>{const av=avatarOf(n),[x,y]=state.pos[n],c=n==="user"?"#7aa2ff":`hsl(${hue(n)},70%,62%)`;
    return `<g class="gnode" data-n="${esc(n)}">${nick(n)!==n?`<title>${esc(n)}</title>`:""}<circle class="halo" cx="${x}" cy="${y}" r="34" fill="url(#g_${esc(n)})"/>
      <circle class="ring" cx="${x}" cy="${y}" r="26" fill="none" stroke="${c}" stroke-width="2" stroke-dasharray="10 7" stroke-linecap="round"/>
      <circle class="core" cx="${x}" cy="${y}" r="21" fill="${av&&av.kind==="image"?`url(#av_${esc(n)})`:"#12151b"}" stroke="${c}" stroke-opacity=".55" stroke-width="2"/>
      ${av&&av.kind==="text"?`<text class="avt" x="${x}" y="${y+6}" text-anchor="middle">${esc(av.text)}</text>`:""}
      <text class="label${av?" under":""}" x="${x}" y="${av?y+38:y+1}" text-anchor="middle">${esc(nick(n))}</text>
      <text class="stat" x="${x}" y="${av?y+49:y+12}" text-anchor="middle"></text></g>`}).join("");
  $("graph").innerHTML=defs+`<g id="boxes">${boxes}</g><g id="edges">${edges}</g><g id="fx"></g>${small?nodes.replace(/r="34"/g,'r="30"').replace(/r="26"/g,'r="23"').replace(/r="21"/g,'r="19"'):nodes}`;
}
function curve(a,b){const[x1,y1]=state.pos[a],[x2,y2]=state.pos[b],mx=(x1+x2)/2,my=(y1+y2)/2,dx=x2-x1,dy=y2-y1,k=.18;return `M${x1},${y1} Q${mx-dy*k},${my+dx*k} ${x2},${y2}`}
/* instant feedback: the user node pulses from the click until the server's first event for the submission (or an error / timeout) */
const PEND={on:null,timer:null,ms:180000},PEND_TXT={task:"submitted · waiting…",ask:"asked · waiting…",steer:"steered · waiting…",resume:"resumed · waiting…"};
function setPending(mode,root){clearTimeout(PEND.timer);const p={mode,root:root||null};PEND.on=p;PEND.timer=setTimeout(()=>clearPending(p),PEND.ms);updateGraph();return p}
function clearPending(p){if(!PEND.on||(p&&PEND.on!==p))return;clearTimeout(PEND.timer);PEND.on=null;updateGraph()}
/* the user node flashes from the click until an agent responds: its first event on that task (ack, thinking, a reply, ...) */
const AGENT_KINDS=new Set(["ack","llm","reply","agent_reply","direct","handover","broadcast","offer","result","artifact","memory","sandbox","mcp","done","error","attention_requested"]);
function pendingSeen(e){const p=PEND.on;if(!p)return false;
  const root=e.root_task_id||e.task_id;
  if(p.mode==="task"&&!p.root&&e.kind==="task"&&e.sender==="user"){p.root=e.task_id;p.armed=true;p.agent=e.recipient;return false}  // the server took it
  if(p.mode==="resume"&&!p.armed&&["task_resumed","task_reopened","user_message"].includes(e.kind)&&root===p.root){p.armed=true;return false}  // the server took the resume
  if(p.mode!=="task"&&!p.armed&&e.kind==="user_message"&&(!p.root||root===p.root)){p.root=root;p.armed=true;p.agent=e.recipient||null;return false}
  if(p.mode==="task"&&p.root&&root===p.root)p.armed=true;  // the POST answered first
  return !!p.armed&&root===p.root&&e.sender&&e.sender!=="user"&&(!p.agent||e.sender===p.agent||e.kind==="ack")&&AGENT_KINDS.has(e.kind)}
function updateGraph(){
  let working=0,att=0;const rp=R.on?replayLoad():null;
  const pendingBy={};for(const who of rp?Object.values(rp.att):Object.values(state.att).map(a=>a.agent))pendingBy[who]=(pendingBy[who]||0)+1;
  const anyAtt=Object.keys(pendingBy).length>0;
  for(const g of document.querySelectorAll(".gnode")){
    const n=g.dataset.n,a=state.agents.find(x=>x.name===n),act=state.activity[n];
    const active=rp?(rp.act[n]||0):(a?a.active:0);
    const waiting=(pendingBy[n]||0)>0||(n==="user"&&anyAtt);
    const nk=rp?null:noticeKind(n);  // finished / error / auto-answered: blinks until dismissed (alerts.py)
    g.classList.toggle("notice",!!nk&&!waiting);for(const k of ["done","error","auto_confirmed"])g.classList.toggle("n-"+k,nk===k);
    const busy=active>0;
    const held=!rp&&a&&a.paused>0;
    const sending=!rp&&n==="user"&&!!PEND.on;
    g.classList.toggle("sending",sending);g.classList.toggle("attention",!!waiting&&!sending);g.classList.toggle("working",!waiting&&busy&&!held);
    let txt=n==="user"?(sending?PEND_TXT[PEND.on.mode]:waiting?"answer needed":""):waiting?"waiting for you":nk&&!busy?NK_TXT[nk]:held?"paused":busy?(act||"working")+(active>1?` ×${active}`:""):"idle";
    g.querySelector(".stat").textContent=txt;working+=busy?1:0;att+=waiting&&n!=="user"?1:0;
  }
  $("gstat").textContent=`${R.on?"replay · ":""}${working} working${att?` · ${att} waiting`:""}`;
}
const ACT={llm:"thinking",sandbox:"running code",mcp:"using tools",handover:"delegating",direct:"asking",broadcast:"searching",artifact:"writing files",memory:"taking notes"};
function particle(from,to,color,dur=900){
  const fx=$("fx");if(!fx||!state.pos[from]||!state.pos[to]||from===to)return;
  const p=document.createElementNS(SVGNS,"path");p.setAttribute("d",curve(from,to));p.setAttribute("class","edge");
  p.style.stroke=color;p.style.strokeOpacity=".55";p.style.strokeWidth="1.5";fx.appendChild(p);
  const dot=document.createElementNS(SVGNS,"circle");dot.setAttribute("r","4");dot.setAttribute("fill",color);dot.setAttribute("filter","url(#glow)");fx.appendChild(dot);
  const tail=document.createElementNS(SVGNS,"circle");tail.setAttribute("r","2.5");tail.setAttribute("fill",color);tail.setAttribute("opacity",".5");fx.appendChild(tail);
  const L=p.getTotalLength(),t0=performance.now();
  (function step(t){const k=Math.min(1,(t-t0)/dur),e=k<.5?2*k*k:1-Math.pow(-2*k+2,2)/2;
    const pt=p.getPointAtLength(L*e),pt2=p.getPointAtLength(L*Math.max(0,e-.06));
    dot.setAttribute("cx",pt.x);dot.setAttribute("cy",pt.y);tail.setAttribute("cx",pt2.x);tail.setAttribute("cy",pt2.y);
    p.style.strokeOpacity=String(.55*(1-k*.6));
    if(k<1)requestAnimationFrame(step);else{dot.remove();tail.remove();let o=.33;const f=()=>{o-=.02;p.style.strokeOpacity=o;if(o>0)requestAnimationFrame(f);else p.remove()};f()}})(t0);
}
function ripple(from,color){
  const fx=$("fx"),c=state.pos[from];if(!fx||!c)return;
  for(let i=0;i<2;i++){const r=document.createElementNS(SVGNS,"circle");r.setAttribute("cx",c[0]);r.setAttribute("cy",c[1]);r.setAttribute("fill","none");r.setAttribute("stroke",color);r.setAttribute("stroke-width","2");fx.appendChild(r);
    const t0=performance.now()+i*220;(function s(t){const k=Math.max(0,Math.min(1,(t-t0)/1100));r.setAttribute("r",22+k*90);r.setAttribute("opacity",1-k);if(k<1)requestAnimationFrame(s);else r.remove()})(performance.now())}
}
function animate(e){
  const color=getComputedStyle(document.documentElement).getPropertyValue(`--${e.kind}`).trim()||"#7aa2ff";
  if(ACT[e.kind]&&state.pos[e.sender])state.activity[e.sender]=ACT[e.kind];
  if(e.kind==="broadcast"){ripple(e.sender,color);Object.keys(state.pos).filter(n=>n!==e.sender&&n!=="user").forEach((n,i)=>setTimeout(()=>particle(e.sender,n,color,1000),i*80))}
  else if(e.kind==="attention_requested"){particle(e.sender,"user",color,1100)}
  else if(e.kind==="attention_resolved"){particle("user",e.recipient,color,900)}
  else if(e.recipient&&e.recipient!=="*"&&["task","direct","reply","offer","handover","result","done"].includes(e.kind))particle(e.sender,e.recipient,color);
  updateGraph();
}

