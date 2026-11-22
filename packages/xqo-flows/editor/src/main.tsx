/* xqo flows editor: FlowGram.AI free-layout canvas + xqo forms, runs and approvals.
   FlowGram owns the canvas (positions, lines, ports); node settings live in a small store here, keyed by node
   id, and are merged into the flow JSON on save. The page talks only to the HQ's /api/flows routes. */
import React, { useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { createRoot } from "react-dom/client";
import {
  EditorRenderer,
  FreeLayoutEditorProvider,
  FreeLayoutPluginContext,
  FreeLayoutProps,
  WorkflowJSON,
  WorkflowNodeEntity,
  WorkflowNodePortsData,
  WorkflowNodeProps,
  WorkflowNodeRenderer,
  useClientContext,
  useNodeRender,
} from "@flowgram.ai/free-layout-editor";
import { createFreeSnapPlugin } from "@flowgram.ai/free-snap-plugin";
import "@flowgram.ai/free-layout-editor/index.css";
import "./editor.css";

type Data = Record<string, any>;
type NodeState = { status: string; title?: string; type?: string; task_id?: string; job_id?: string; worker?: string;
  output?: Data; error?: string; question?: string; port?: string };
type RunRec = { id: string; flow: string; status: string; started: string; ended?: string; result: string; error: string;
  where: string; inputs: Data; nodes: Record<string, NodeState> };

/* ---------------- tiny store: node data, the shown run, the selection ---------------- */
const store = {
  data: {} as Record<string, Data>,
  run: null as RunRec | null,
  selected: "" as string,
  v: 0,
  subs: new Set<() => void>(),
  set(fn: () => void) { fn(); this.v++; this.subs.forEach((s) => s()); },
};
const subscribe = (f: () => void) => { store.subs.add(f); return () => store.subs.delete(f); };
const useStore = () => useSyncExternalStore(subscribe, () => store.v);

/* ---------------- node types ---------------- */
const TYPES: Record<string, { label: string; icon: string; color: string; hint: string }> = {
  start: { label: "Start", icon: "▶", color: "#68d391", hint: "where a run begins; lists the flow's inputs" },
  agent: { label: "Agent", icon: "🤖", color: "#7aa2ff", hint: "an xqo task for one of this HQ's agents" },
  remote: { label: "Remote", icon: "🛰", color: "#4fd1c5", hint: "a task for a headless worker with matching labels" },
  condition: { label: "Condition", icon: "◇", color: "#ecc94b", hint: "picks one branch by rules on earlier results" },
  approval: { label: "Approval", icon: "✋", color: "#ed64a6", hint: "waits for your approve or reject" },
  end: { label: "End", icon: "■", color: "#a0aec0", hint: "the run's result" },
};
const OPS = ["contains", "not_contains", "equals", "not_equals", "matches", "empty", "not_empty", "gt", "lt"];

function defaults(type: string, n: number): Data {
  switch (type) {
    case "start": return { title: "Start", inputs: [] };
    case "agent": return { title: `Agent ${n}`, agent: "", prompt: "", unattended: true };
    case "remote": return { title: `Remote ${n}`, labels: [], worker: "", agent: "", prompt: "" };
    case "condition": return { title: `Condition ${n}`, conditions: [{ key: "if_0", left: "", op: "contains", right: "" }] };
    case "approval": return { title: `Approval ${n}`, question: "Continue?", default: "reject" };
    default: return { title: "End", output: "" };
  }
}
const outPorts = (type: string, d: Data): string[] =>
  type === "condition" ? [...(d.conditions || []).map((c: Data) => c.key), "else"]
    : type === "approval" ? ["approve", "reject"] : [];

const registries = Object.keys(TYPES).map((type) => ({
  type,
  meta: {
    defaultExpanded: true,
    ...(type === "start" ? { isStart: true, deleteDisable: true, copyDisable: true, defaultPorts: [{ type: "output" }] } : {}),
    ...(type === "end" ? { defaultPorts: [{ type: "input" }] } : {}),
    ...(type === "condition" || type === "approval" ? { useDynamicPort: true, defaultPorts: [{ type: "input" }] } : {}),
    size: { width: 260, height: 90 },
  },
}));

/* ---------------- API ---------------- */
async function api(path: string, opts: RequestInit & { json?: any } = {}) {
  const r = await fetch(path, { ...opts, headers: { "Content-Type": "application/json" },
    body: opts.json !== undefined ? JSON.stringify(opts.json) : opts.body });
  const text = await r.text();
  const body = text ? JSON.parse(text) : null;
  if (!r.ok) throw new Error((body && (typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail))) || r.statusText);
  return body;
}

/* ---------------- canvas node ---------------- */
function NodeView(props: WorkflowNodeProps) {
  useStore();
  const { node } = props;
  const type = String(node.flowNodeType);
  const d = store.data[node.id] || {};
  const t = TYPES[type] || TYPES.agent;
  const st = store.run?.nodes?.[node.id];
  const ports = outPorts(type, d);
  const ref = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {  // condition rules / approval ports are DOM ports: re-read them after a change
    if (ports.length) node.getData(WorkflowNodePortsData)?.updateDynamicPorts();
  }, [ports.join("|")]);
  const summary = type === "agent" || type === "remote"
    ? `${d.agent || "default agent"}${type === "remote" ? ` · ${d.worker || (d.labels || []).join(", ") || "any worker"}` : ""}`
    : type === "start" ? ((d.inputs || []).map((i: Data) => i.name).join(", ") || "no inputs")
    : type === "approval" ? (d.question || "") : type === "end" ? (d.output || "(last step's result)") : "";
  return (
    <WorkflowNodeRenderer node={node} className={`xn ${st ? "st-" + st.status : ""} ${store.selected === node.id ? "sel" : ""}`}
      portPrimaryColor={t.color} portSecondaryColor="#4a5162">
      <div ref={ref} className="xn-box" style={{ borderTopColor: t.color }}
        onMouseDown={() => store.set(() => { store.selected = node.id; })}>
        <div className="xn-head"><span className="xn-ic">{t.icon}</span><b>{d.title || node.id}</b>
          {st && <span className={`xn-st st-${st.status}`}>{st.status}</span>}</div>
        <div className="xn-sum">{summary}</div>
        {type === "agent" || type === "remote" ? <div className="xn-prompt">{(d.prompt || "").slice(0, 90)}</div> : null}
        {ports.length > 0 && <div className="xn-ports">{ports.map((p) => (
          <div key={p} className={`xn-port ${st?.port === p ? "taken" : ""}`}>
            <span>{p}</span><div className="xn-dot" data-port-id={p} data-port-type="output" />
          </div>))}</div>}
      </div>
    </WorkflowNodeRenderer>
  );
}

/* ---------------- forms ---------------- */
function Field(p: { label: string; children: React.ReactNode; hint?: string }) {
  return <label className="fld"><span>{p.label}</span>{p.children}{p.hint && <small>{p.hint}</small>}</label>;
}

function NodeForm({ id, type, agents, workers }: { id: string; type: string; agents: Data[]; workers: Data }) {
  useStore();
  const d = store.data[id] || {};
  const up = (patch: Data) => store.set(() => { store.data[id] = { ...store.data[id], ...patch }; markDirty(); });
  const labels = Array.from(new Set((workers.workers || []).flatMap((w: Data) => w.labels)));
  return (
    <div className="form">
      <div className="form-h"><span style={{ color: TYPES[type]?.color }}>{TYPES[type]?.icon}</span> {TYPES[type]?.label}
        <small>{id}</small></div>
      <p className="hint">{TYPES[type]?.hint}</p>
      <Field label="Title"><input value={d.title || ""} onChange={(e) => up({ title: e.target.value })} /></Field>
      {type === "start" && <>
        <div className="sub">Inputs <small>use them as {"{{inputs.NAME}}"}</small></div>
        {(d.inputs || []).map((inp: Data, i: number) => (
          <div className="row" key={i}>
            <input placeholder="name" value={inp.name} onChange={(e) => { const a = [...d.inputs]; a[i] = { ...inp, name: e.target.value.toLowerCase().replace(/[^a-z0-9_-]/g, "") }; up({ inputs: a }); }} />
            <input placeholder="default" value={inp.default || ""} onChange={(e) => { const a = [...d.inputs]; a[i] = { ...inp, default: e.target.value }; up({ inputs: a }); }} />
            <button className="x" onClick={() => up({ inputs: d.inputs.filter((_: any, j: number) => j !== i) })}>✕</button>
          </div>))}
        <button className="ghost" onClick={() => up({ inputs: [...(d.inputs || []), { name: `input${(d.inputs || []).length + 1}`, default: "" }] })}>+ input</button>
      </>}
      {(type === "agent" || type === "remote") && <>
        <Field label="Agent" hint={type === "remote" ? "an agent on the worker (empty: the worker's first agent)" : "empty: the HQ's first agent"}>
          {type === "agent"
            ? <select value={d.agent || ""} onChange={(e) => up({ agent: e.target.value })}>
                <option value="">(default agent)</option>
                {agents.map((a) => <option key={a.name} value={a.name}>{a.nickname ? `${a.nickname} (${a.name})` : a.name}{a.department && a.department !== "hq" ? ` · ${a.department}` : ""}</option>)}
                {d.agent && !agents.some((a) => a.name === d.agent) && <option value={d.agent}>{d.agent} (unknown)</option>}
              </select>
            : <input value={d.agent || ""} placeholder="(worker's default)" onChange={(e) => up({ agent: e.target.value })} />}
        </Field>
        <Field label="Prompt" hint="{{inputs.NAME}}, {{NODE_ID.result}} fill in earlier results">
          <textarea rows={8} value={d.prompt || ""} onChange={(e) => up({ prompt: e.target.value })} /></Field>
        {type === "remote" && <>
          <Field label="Labels" hint={labels.length ? `known: ${labels.join(", ")}` : "no workers listed on this HQ yet"}>
            <input value={(d.labels || []).join(", ")} onChange={(e) => up({ labels: e.target.value.split(",").map((x) => x.trim()).filter(Boolean) })} /></Field>
          <Field label="Worker" hint="only this worker (optional)">
            <select value={d.worker || ""} onChange={(e) => up({ worker: e.target.value })}>
              <option value="">(any with the labels)</option>
              {(workers.workers || []).map((w: Data) => <option key={w.name} value={w.name}>{w.name}{w.online ? "" : " (offline)"}</option>)}
            </select></Field>
          <Field label="Timeout (seconds)"><input type="number" min={0} value={d.timeout || ""} onChange={(e) => up({ timeout: e.target.value ? Number(e.target.value) : undefined })} /></Field>
        </>}
        <label className="chk"><input type="checkbox" checked={d.unattended !== false} onChange={(e) => up({ unattended: e.target.checked })} /> unattended (questions take their default answer)</label>
        <label className="chk"><input type="checkbox" checked={!!d.continue_on_error} onChange={(e) => up({ continue_on_error: e.target.checked })} /> continue the flow if this step fails</label>
      </>}
      {type === "condition" && <>
        <div className="sub">Rules <small>the first rule that holds picks its branch; otherwise "else"</small></div>
        {(d.conditions || []).map((c: Data, i: number) => {
          const set = (patch: Data) => { const a = [...d.conditions]; a[i] = { ...c, ...patch }; up({ conditions: a }); };
          return <div className="rule" key={i}>
            <div className="row"><input className="key" value={c.key} onChange={(e) => set({ key: e.target.value.replace(/[^A-Za-z0-9_-]/g, "") })} />
              <button className="x" disabled={d.conditions.length < 2} onClick={() => up({ conditions: d.conditions.filter((_: any, j: number) => j !== i) })}>✕</button></div>
            <input placeholder="{{node.result}}" value={c.left} onChange={(e) => set({ left: e.target.value })} />
            <div className="row"><select value={c.op} onChange={(e) => set({ op: e.target.value })}>{OPS.map((o) => <option key={o}>{o}</option>)}</select>
              {!["empty", "not_empty"].includes(c.op) && <input placeholder="value" value={c.right} onChange={(e) => set({ right: e.target.value })} />}</div>
          </div>;
        })}
        <button className="ghost" onClick={() => { const keys = new Set((d.conditions || []).map((c: Data) => c.key)); let n = 0; while (keys.has(`if_${n}`)) n++;
          up({ conditions: [...(d.conditions || []), { key: `if_${n}`, left: "", op: "contains", right: "" }] }); }}>+ rule</button>
      </>}
      {type === "approval" && <>
        <Field label="Question"><textarea rows={4} value={d.question || ""} onChange={(e) => up({ question: e.target.value })} /></Field>
        <Field label="Timeout (seconds)" hint="empty: the HQ's flows.approval_timeout">
          <input type="number" min={0} value={d.timeout || ""} onChange={(e) => up({ timeout: e.target.value ? Number(e.target.value) : undefined })} /></Field>
        <Field label="After the timeout"><select value={d.default || "reject"} onChange={(e) => up({ default: e.target.value })}>
          <option value="reject">reject</option><option value="approve">approve</option></select></Field>
      </>}
      {type === "end" && <Field label="Output" hint="empty: the last step's result">
        <textarea rows={5} value={d.output || ""} onChange={(e) => up({ output: e.target.value })} /></Field>}
      <RunNodeInfo id={id} />
    </div>
  );
}

function RunNodeInfo({ id }: { id: string }) {
  useStore();
  const st = store.run?.nodes?.[id];
  if (!st) return null;
  const out = st.output?.result ?? "";
  return <div className="ninfo">
    <div className="sub">In run {store.run!.id}: <span className={`xn-st st-${st.status}`}>{st.status}</span></div>
    {st.task_id && <div>task: <a href={`/#${encodeURIComponent(st.task_id)}`} target="_blank" rel="noreferrer">{st.task_id}</a>
      {st.worker ? ` on worker ${st.worker}` : ""}</div>}
    {st.error && <pre className="err">{st.error}</pre>}
    {out && <pre>{String(out).slice(0, 4000)}</pre>}
  </div>;
}

/* ---------------- dirty flag ---------------- */
let dirty = false;
const dirtySubs = new Set<() => void>();
function markDirty(v = true) { dirty = v; dirtySubs.forEach((f) => f()); }
const useDirty = () => useSyncExternalStore((f) => { dirtySubs.add(f); return () => dirtySubs.delete(f); }, () => dirty);

/* ---------------- toolbar, palette, side panel ---------------- */
function Palette() {
  const ctx = useClientContext() as FreeLayoutPluginContext;
  const add = (type: string) => {
    const nodes = ctx.document.getAllNodes();
    let n = 1;
    while (ctx.document.getNode(`${type}_${n}`)) n++;
    const id = `${type}_${n}`;
    const view = ctx.playground.config.getViewport?.() as any;
    const pos = view ? { x: view.x + view.width / 2 - 130, y: view.y + view.height / 2 - 45 + (nodes.length % 5) * 24 } : { x: 400, y: 200 };
    store.set(() => { store.data[id] = defaults(type, n); store.selected = id; });
    ctx.document.createWorkflowNodeByType(type, pos, { id, data: store.data[id] } as any);
    markDirty();
  };
  return <div className="palette">{["agent", "remote", "condition", "approval", "end"].map((t) => (
    <button key={t} title={TYPES[t].hint} onClick={() => add(t)} style={{ borderLeftColor: TYPES[t].color }}>
      {TYPES[t].icon} {TYPES[t].label}</button>))}
    <span className="sep" />
    <button className="ghost" title="Fit the flow into view" onClick={() => ctx.tools.fitView()}>⤢ fit</button>
    <button className="ghost" title="Arrange nodes automatically" onClick={() => { ctx.tools.autoLayout(); markDirty(); }}>⇶ arrange</button>
  </div>;
}

function toDoc(ctx: FreeLayoutPluginContext, meta: Data): Data {
  const json = ctx.document.toJSON() as WorkflowJSON;
  const nodes = json.nodes.map((n: any) => ({ id: n.id, type: n.type, meta: n.meta, data: store.data[n.id] || n.data || {} }));
  const edges = json.edges.map((e: any) => ({ sourceNodeID: e.sourceNodeID, targetNodeID: e.targetNodeID,
    ...(e.sourcePortID ? { sourcePortID: e.sourcePortID } : {}) }));
  return { ...meta, nodes, edges };
}

function App() {
  useStore();
  const isDirty = useDirty();
  const ctxRef = useRef<FreeLayoutPluginContext | null>(null);
  const [flows, setFlows] = useState<Data[]>([]);
  const [name, setName] = useState("");
  const [meta, setMeta] = useState<Data>({ title: "", description: "" });
  const [agents, setAgents] = useState<Data[]>([]);
  const [workers, setWorkers] = useState<Data>({ workers: [] });
  const [runs, setRuns] = useState<Data[]>([]);
  const [msg, setMsg] = useState<{ text: string; bad?: boolean } | null>(null);
  const [tab, setTab] = useState<"node" | "runs">("node");
  const [asking, setAsking] = useState<Data | null>(null);
  const [loadKey, setLoadKey] = useState(0);
  const [initial, setInitial] = useState<WorkflowJSON | null>(null);

  const sel = store.selected;
  useEffect(() => { if (sel) setTab("node"); }, [sel]);
  const say = (text: string, bad = false) => { setMsg({ text, bad }); if (!bad) setTimeout(() => setMsg(null), 4000); };
  const refreshFlows = () => api("/api/flows").then(setFlows).catch((e) => say(e.message, true));

  useEffect(() => {
    refreshFlows();
    api("/api/agents").then(setAgents).catch(() => {});
    api("/api/flows/workers").then(setWorkers).catch(() => {});
    const onHash = () => openFromHash();
    window.addEventListener("hashchange", onHash);
    openFromHash();
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  function openFromHash() {
    const [f, r] = decodeURIComponent(location.hash.slice(1)).split("/");
    if (f && f !== name) load(f, r);
    else if (r) showRun(r);
  }

  async function load(n: string, runId?: string) {
    if (dirty && !confirm("Discard unsaved changes?")) return;
    try {
      const doc = n === "__new__" ? newDoc() : await api(`/api/flows/${encodeURIComponent(n)}`);
      store.set(() => { store.data = {}; for (const nd of doc.nodes) store.data[nd.id] = { ...nd.data }; store.run = null; store.selected = ""; });
      setMeta({ title: doc.title || "", description: doc.description || "" });
      setName(n === "__new__" ? "" : n);
      setInitial({ nodes: doc.nodes.map((nd: Data, i: number) => ({ id: nd.id, type: nd.type, data: nd.data,
        meta: nd.meta?.position ? nd.meta : { position: { x: i * 340, y: 0 } } })), edges: doc.edges } as any);
      setLoadKey((k) => k + 1);
      markDirty(n === "__new__");
      if (n !== "__new__") {
        history.replaceState(null, "", `#${encodeURIComponent(n)}${runId ? "/" + encodeURIComponent(runId) : ""}`);
        refreshRuns(n);
        if (runId) showRun(runId);
      }
    } catch (e: any) { say(e.message, true); }
  }

  function newDoc() {
    return { title: "New flow", description: "", nodes: [
      { id: "start_0", type: "start", meta: { position: { x: 0, y: 0 } }, data: { title: "Start", inputs: [{ name: "topic", default: "" }] } },
      { id: "agent_1", type: "agent", meta: { position: { x: 360, y: 0 } }, data: { title: "Agent 1", agent: "", prompt: "Work on {{inputs.topic}}" } },
      { id: "end_0", type: "end", meta: { position: { x: 720, y: 0 } }, data: { title: "End", output: "{{agent_1.result}}" } }],
      edges: [{ sourceNodeID: "start_0", targetNodeID: "agent_1" }, { sourceNodeID: "agent_1", targetNodeID: "end_0" }] };
  }

  async function save(): Promise<boolean> {
    const ctx = ctxRef.current;
    if (!ctx) return false;
    let n = name;
    if (!n) {
      n = (prompt("Name for this flow (a-z, 0-9, - and _):", (meta.title || "flow").toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "")) || "").trim();
      if (!n) return false;
    }
    try {
      const saved = await api(`/api/flows/${encodeURIComponent(n)}`, { method: "PUT", json: toDoc(ctx, meta) });
      markDirty(false);
      if (n !== name) { setName(n); history.replaceState(null, "", `#${encodeURIComponent(n)}`); }
      refreshFlows();
      say(saved.warnings?.length ? `Saved. Before running: ${saved.warnings.join("; ")}` : "Saved", !!saved.warnings?.length);
      return true;
    } catch (e: any) { say(e.message, true); return false; }
  }

  async function refreshRuns(n = name) {
    if (!n) return;
    try { setRuns(await api(`/api/flows/runs?flow=${encodeURIComponent(n)}&limit=30`)); } catch { /* list stays */ }
  }

  const poll = useRef<number | null>(null);
  async function showRun(id: string) {
    if (poll.current) clearTimeout(poll.current);
    const tick = async () => {
      try {
        const r: RunRec = await api(`/api/flows/runs/${encodeURIComponent(id)}`);
        store.set(() => { store.run = r; });
        if (!["done", "error", "cancelled", "interrupted"].includes(r.status)) poll.current = window.setTimeout(tick, 800);
        else refreshRuns(r.flow);
      } catch (e: any) { say(e.message, true); }
    };
    tick();
    setTab("runs");
  }

  async function run(remote?: Data) {
    if (dirty && !(await save())) return;
    const start = Object.entries(store.data).find(([id]) => ctxRef.current?.document.getNode(id)?.flowNodeType === "start");
    const inputs = (start?.[1].inputs || []) as Data[];
    setAsking({ inputs: Object.fromEntries(inputs.map((i) => [i.name, i.default || ""])), remote: remote || null, where: "hq", labels: "" });
  }

  async function startRun() {
    const a = asking!;
    setAsking(null);
    try {
      const body: Data = { inputs: a.inputs };
      if (a.where === "worker") { body.remote = true; body.labels = a.labels.split(",").map((x: string) => x.trim()).filter(Boolean); body.worker = a.worker || ""; }
      const r = await api(`/api/flows/${encodeURIComponent(name)}/run`, { method: "POST", json: body });
      history.replaceState(null, "", `#${encodeURIComponent(name)}/${encodeURIComponent(r.id)}`);
      showRun(r.id);
      refreshRuns();
    } catch (e: any) { say(e.message, true); }
  }

  async function del() {
    if (!name || !confirm(`Delete flow ${name}? Its runs stay in the history.`)) return;
    try { await api(`/api/flows/${encodeURIComponent(name)}`, { method: "DELETE" }); markDirty(false); setName(""); setInitial(null); history.replaceState(null, "", "#"); refreshFlows(); }
    catch (e: any) { say(e.message, true); }
  }

  const editorProps: FreeLayoutProps = useMemo(() => ({
    background: true,
    readonly: false,
    initialData: initial || { nodes: [], edges: [] },
    nodeRegistries: registries as any,
    getNodeDefaultRegistry: (type: any) => ({ type, meta: { defaultExpanded: true } }) as any,
    materials: { renderDefaultNode: NodeView as any },
    nodeEngine: { enable: false },
    history: { enable: true, enableChangeNode: true },
    canAddLine: (_ctx, from, to) => from.node !== to.node && to.node.flowNodeType !== "start",
    canDeleteNode: (_ctx, node) => node.flowNodeType !== "start",
    onContentChange: () => markDirty(),
    onLoad: (ctx) => { ctxRef.current = ctx; },
    onAllLayersRendered: (ctx: FreeLayoutPluginContext) => {
      ctxRef.current = ctx;
      setTimeout(() => ctx.tools.fitView(false), 50);
      (ctx.selection as any)?.onSelectionChanged?.(() => {
        const node = (ctx.selection.selection || []).find((e: any) => e instanceof WorkflowNodeEntity) as any;
        store.set(() => { store.selected = node ? node.id : ""; });
      });
    },
    plugins: () => [createFreeSnapPlugin({})],
  } as FreeLayoutProps), [loadKey]);

  const selType = store.selected && ctxRef.current?.document.getNode(store.selected)?.flowNodeType;
  const run_ = store.run;
  const waiting = run_ ? Object.entries(run_.nodes).filter(([, s]) => s.status === "waiting") : [];

  return <div className="app">
    <header>
      <a className="home" href="/" title="Back to the dashboard">◀ xqo</a>
      <b>Flows</b>
      <select value={name} onChange={(e) => load(e.target.value)}>
        <option value="" disabled>{flows.length ? "open a flow…" : "no flows yet"}</option>
        {flows.map((f) => <option key={f.name} value={f.name}>{f.title} ({f.name})</option>)}
      </select>
      <button className="ghost" onClick={() => load("__new__")}>+ New</button>
      {initial && <>
        <input className="title" value={meta.title} placeholder="Title" onChange={(e) => { setMeta({ ...meta, title: e.target.value }); markDirty(); }} />
        <span className="spacer" />
        {isDirty && <span className="dirty">unsaved</span>}
        <button onClick={save}>💾 Save</button>
        <button onClick={() => run()} disabled={!name && !isDirty}>▶ Run</button>
        {name && <button className="ghost danger" onClick={del}>Delete</button>}
      </>}
    </header>
    {msg && <div className={`msg ${msg.bad ? "bad" : ""}`} onClick={() => setMsg(null)}>{msg.text}</div>}
    {!initial ? <div className="empty">
      <h2>Visual flows</h2>
      <p>Chain this HQ's agents into a flow: steps, conditions, your approvals, and remote steps on headless workers.</p>
      <div className="cards">{flows.map((f) => <div key={f.name} className="card" onClick={() => load(f.name)}>
        <b>{f.title}</b><small>{f.name} · {f.nodes} nodes{f.last_run ? ` · last run ${f.last_run.status}` : ""}</small>
        <p>{f.description}</p></div>)}
        <div className="card new" onClick={() => load("__new__")}><b>+ New flow</b><p>Start, one agent step, end.</p></div></div>
    </div> :
      <div className="main">
        <FreeLayoutEditorProvider key={loadKey} {...editorProps}>
          <div className="canvas"><Palette /><EditorRenderer className="fg" /></div>
          <aside>
            <div className="tabs"><button className={tab === "node" ? "on" : ""} onClick={() => setTab("node")}>Step</button>
              <button className={tab === "runs" ? "on" : ""} onClick={() => { setTab("runs"); refreshRuns(); }}>Runs{run_ && !["done", "error", "cancelled", "interrupted"].includes(run_.status) ? " ●" : ""}</button></div>
            {tab === "node" ? (selType ? <NodeForm key={store.selected} id={store.selected} type={String(selType)} agents={agents} workers={workers} />
              : <div className="form"><div className="form-h">Flow</div>
                <Field label="Description"><textarea rows={4} value={meta.description} onChange={(e) => { setMeta({ ...meta, description: e.target.value }); markDirty(); }} /></Field>
                <p className="hint">Click a step to edit it. Drag from a node's right edge to another node's left edge to connect them;
                  a condition or approval connects from each named port. Several lines out of one step run in parallel.</p>
                <p className="hint">Templates: <code>{"{{inputs.topic}}"}</code>, <code>{"{{step_id.result}}"}</code>.</p></div>)
              : <div className="runs">
                {run_ && <div className="runcard">
                  <div className="row"><b>{run_.id}</b><span className={`xn-st st-${run_.status}`}>{run_.status}</span>
                    {!["done", "error", "cancelled", "interrupted"].includes(run_.status) &&
                      <button className="ghost danger" onClick={() => api(`/api/flows/runs/${run_.id}/cancel`, { method: "POST" }).then(() => showRun(run_.id)).catch((e) => say(e.message, true))}>Cancel</button>}
                    <button className="ghost" onClick={() => store.set(() => { store.run = null; })}>hide</button></div>
                  <small>{run_.where !== "hq" ? `on ${run_.where} · ` : ""}{new Date(run_.started).toLocaleString()}</small>
                  {waiting.map(([nid, s]) => <div key={nid} className="approve">
                    <b>✋ {s.title}</b><pre>{s.question}</pre>
                    <div className="row"><button onClick={() => api(`/api/flows/runs/${run_.id}/approve`, { method: "POST", json: { node: nid, approve: true } }).then(() => showRun(run_.id))}>Approve</button>
                      <button className="ghost danger" onClick={() => api(`/api/flows/runs/${run_.id}/approve`, { method: "POST", json: { node: nid, approve: false } }).then(() => showRun(run_.id))}>Reject</button></div></div>)}
                  {run_.error && <pre className="err">{run_.error}</pre>}
                  {run_.result && <pre>{run_.result}</pre>}
                </div>}
                <div className="sub">Recent runs</div>
                {runs.map((r) => <div key={r.id} className={`runrow ${run_?.id === r.id ? "on" : ""}`} onClick={() => showRun(r.id)}>
                  <span className={`xn-st st-${r.status}`}>{r.status}</span><span>{new Date(r.started).toLocaleString()}</span>
                  <small>{r.where !== "hq" ? r.where : ""}</small></div>)}
                {!runs.length && <p className="hint">No runs yet. Press ▶ Run.</p>}
              </div>}
          </aside>
        </FreeLayoutEditorProvider>
      </div>}
    {asking && <div className="modal" onClick={(e) => { if (e.target === e.currentTarget) setAsking(null); }}><div className="box">
      <h3>Run {meta.title || name}</h3>
      {Object.keys(asking.inputs).map((k) => <Field key={k} label={k}>
        <textarea rows={2} value={asking.inputs[k]} onChange={(e) => setAsking({ ...asking, inputs: { ...asking.inputs, [k]: e.target.value } })} /></Field>)}
      {!Object.keys(asking.inputs).length && <p className="hint">This flow has no inputs.</p>}
      <Field label="Where">
        <select value={asking.where} onChange={(e) => setAsking({ ...asking, where: e.target.value })}>
          <option value="hq">on this HQ (remote steps still go to workers)</option>
          <option value="worker" disabled={!(workers.workers || []).length}>the whole flow on a worker</option>
        </select></Field>
      {asking.where === "worker" && <>
        <Field label="Labels" hint="the worker needs all of them"><input value={asking.labels} onChange={(e) => setAsking({ ...asking, labels: e.target.value })} /></Field>
        <Field label="Worker"><select value={asking.worker || ""} onChange={(e) => setAsking({ ...asking, worker: e.target.value })}>
          <option value="">(any with the labels)</option>
          {(workers.workers || []).map((w: Data) => <option key={w.name} value={w.name}>{w.name}{w.online ? "" : " (offline)"}</option>)}</select></Field>
      </>}
      <div className="row end"><button className="ghost" onClick={() => setAsking(null)}>Cancel</button><button onClick={startRun}>▶ Start</button></div>
    </div></div>}
  </div>;
}

createRoot(document.getElementById("root")!).render(<App />);
