"""Flow documents: what the editor saves and the engine runs.

A flow is the FlowGram.AI free-layout JSON (``nodes`` + ``edges``) plus a name, a title and a description:

    {"name": "research", "title": "Research and write", "description": "",
     "nodes": [{"id": "start_0", "type": "start", "meta": {"position": {"x": 0, "y": 0}},
                "data": {"title": "Start", "inputs": [{"name": "topic", "default": "solar"}]}},
               {"id": "research", "type": "agent", "data": {"agent": "researcher", "prompt": "Find facts on {{inputs.topic}}"}},
               {"id": "end_0", "type": "end", "data": {"output": "{{research.result}}"}}],
     "edges": [{"sourceNodeID": "start_0", "targetNodeID": "research"},
               {"sourceNodeID": "research", "targetNodeID": "end_0"}]}

Node types (``data`` keys):
  start      inputs: [{name, default}]                       one per flow; where a run begins
  agent      agent, prompt, unattended (default true), continue_on_error
             runs an xqo task on this HQ (or on the worker that runs the whole flow)
  remote     labels: [..], worker, agent, prompt, timeout, continue_on_error
             sends the step to a headless worker (xqo-flows-runner) whose labels match
  condition  conditions: [{key, left, op, right}]           ports: each key, then "else"
  approval   question, timeout, default ("approve" | "reject")   ports: "approve", "reject"
  end        output                                         the run's result (templates allowed)

Templates: ``{{inputs.NAME}}``, ``{{NODE_ID}}`` / ``{{NODE_ID.result}}``, ``{{NODE_ID.status}}``,
``{{NODE_ID.task_id}}``. Unknown references become empty text.

Edges: ``sourceNodeID`` -> ``targetNodeID``; an edge leaving a condition or approval node names its port in
``sourcePortID``. Flows are acyclic (no loops); several edges leaving a node run their targets in parallel, and a
node with several incoming edges waits for all branches that are still alive (dead-path elimination).
"""
from __future__ import annotations

import re
from typing import Any

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
RESERVED = {"runs", "workers", "approvals", "new"}
NODE_TYPES = {"start", "agent", "remote", "condition", "approval", "end"}
OPS = {"contains", "not_contains", "equals", "not_equals", "matches", "empty", "not_empty", "gt", "lt"}
UNARY = {"empty", "not_empty"}
APPROVAL_PORTS = ("approve", "reject")
TEMPLATE_RE = re.compile(r"\{\{\s*([A-Za-z0-9_\-]+)(?:\.([A-Za-z0-9_\-]+))?\s*\}\}")
MAX_NODES = 200
MAX_TEXT = 100_000


class FlowError(ValueError):
    """A flow document that cannot be saved or run (shown to the user as is)."""


def _text(v: Any, where: str, limit: int = MAX_TEXT) -> str:
    if v is None:
        return ""
    if not isinstance(v, str):
        raise FlowError(f"{where} must be text")
    if len(v) > limit:
        raise FlowError(f"{where} is longer than {limit} characters")
    return v


def ports(node: dict) -> list[str]:
    """The named output ports of a branching node ([] = one unnamed port)."""
    t = node.get("type")
    if t == "condition":
        return [c["key"] for c in node.get("data", {}).get("conditions", [])] + ["else"]
    if t == "approval":
        return list(APPROVAL_PORTS)
    return []


def validate(doc: Any, name: str | None = None) -> dict:
    """Check a flow and return a normalized copy (raises FlowError). Positions and unknown ``meta`` / ``data``
    keys from the editor are kept, so a save never loses layout."""
    if not isinstance(doc, dict):
        raise FlowError("a flow must be a JSON object")
    name = name or doc.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name) or name in RESERVED:
        raise FlowError("the flow name must be 1-64 characters: a-z, 0-9, '-', '_' (not runs, workers, approvals, new)")
    nodes, edges = doc.get("nodes"), doc.get("edges", [])
    if not isinstance(nodes, list) or not nodes:
        raise FlowError("a flow needs nodes")
    if not isinstance(edges, list):
        raise FlowError("edges must be a list")
    if len(nodes) > MAX_NODES:
        raise FlowError(f"a flow has at most {MAX_NODES} nodes")
    out_nodes, ids = [], set()
    for i, n in enumerate(nodes):
        if not isinstance(n, dict):
            raise FlowError(f"node {i} must be an object")
        nid, t = n.get("id"), n.get("type")
        if not isinstance(nid, str) or not re.match(r"^[A-Za-z0-9_\-]{1,64}$", nid):
            raise FlowError(f"node {i}: id must be 1-64 characters: letters, digits, '-', '_'")
        if nid in ids or nid == "inputs":
            raise FlowError(f"node id {nid!r} is used twice" if nid in ids else "'inputs' is not a node id")
        if t not in NODE_TYPES:
            raise FlowError(f"node {nid}: unknown type {t!r} (known: {', '.join(sorted(NODE_TYPES))})")
        ids.add(nid)
        data = n.get("data") or {}
        if not isinstance(data, dict):
            raise FlowError(f"node {nid}: data must be an object")
        data = dict(data)
        data["title"] = _text(data.get("title"), f"node {nid}: title", 200) or nid
        if t == "start":
            inputs = data.get("inputs") or []
            if not isinstance(inputs, list):
                raise FlowError(f"node {nid}: inputs must be a list")
            seen = set()
            clean = []
            for x in inputs:
                x = {"name": x} if isinstance(x, str) else x
                if not isinstance(x, dict) or not isinstance(x.get("name"), str) or not NAME_RE.match(x["name"].lower()):
                    raise FlowError(f"node {nid}: every input needs a name (a-z, 0-9, '-', '_')")
                if x["name"] in seen:
                    raise FlowError(f"node {nid}: input {x['name']!r} is listed twice")
                seen.add(x["name"])
                clean.append({**x, "default": _text(x.get("default"), f"input {x['name']}: default")})
            data["inputs"] = clean
        elif t in ("agent", "remote"):
            data["prompt"] = _text(data.get("prompt"), f"node {nid}: prompt")
            if not data["prompt"].strip():
                raise FlowError(f"node {nid}: the prompt is empty")
            data["agent"] = _text(data.get("agent"), f"node {nid}: agent", 100).strip()
            if t == "remote":
                labels = data.get("labels") or []
                if isinstance(labels, str):
                    labels = [x.strip() for x in labels.split(",") if x.strip()]
                if not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
                    raise FlowError(f"node {nid}: labels must be a list of text")
                data["labels"] = labels
                data["worker"] = _text(data.get("worker"), f"node {nid}: worker", 100).strip()
                if data.get("timeout") is not None and not isinstance(data["timeout"], (int, float)):
                    raise FlowError(f"node {nid}: timeout must be seconds")
        elif t == "condition":
            conds = data.get("conditions") or []
            if not isinstance(conds, list) or not conds:
                raise FlowError(f"node {nid}: a condition needs at least one rule")
            keys, clean = set(), []
            for j, c in enumerate(conds):
                if not isinstance(c, dict):
                    raise FlowError(f"node {nid}: rule {j} must be an object")
                key = c.get("key") or f"if_{j}"
                if not isinstance(key, str) or key in keys or key == "else":
                    raise FlowError(f"node {nid}: rule keys must be unique and not 'else'")
                op = c.get("op", "contains")
                if op not in OPS:
                    raise FlowError(f"node {nid}: unknown operator {op!r} (known: {', '.join(sorted(OPS))})")
                if op == "matches":
                    try:
                        re.compile(_text(c.get("right"), f"node {nid}: pattern", 1000))
                    except re.error as e:
                        raise FlowError(f"node {nid}: bad pattern: {e}") from None
                keys.add(key)
                clean.append({**c, "key": key, "op": op, "left": _text(c.get("left"), f"node {nid}: left"),
                              "right": _text(c.get("right"), f"node {nid}: right")})
            data["conditions"] = clean
        elif t == "approval":
            data["question"] = _text(data.get("question"), f"node {nid}: question") or "Continue?"
            if data.get("default", "reject") not in APPROVAL_PORTS:
                raise FlowError(f"node {nid}: default must be approve or reject")
            if data.get("timeout") is not None and not isinstance(data["timeout"], (int, float)):
                raise FlowError(f"node {nid}: timeout must be seconds")
        elif t == "end":
            data["output"] = _text(data.get("output"), f"node {nid}: output")
        out_nodes.append({**n, "id": nid, "type": t, "data": data})
    starts = [n for n in out_nodes if n["type"] == "start"]
    if len(starts) != 1:
        raise FlowError("a flow needs exactly one start node")
    by_id = {n["id"]: n for n in out_nodes}
    out_edges, seen_edges = [], set()
    for i, e in enumerate(edges):
        if not isinstance(e, dict):
            raise FlowError(f"edge {i} must be an object")
        s, t, port = e.get("sourceNodeID"), e.get("targetNodeID"), e.get("sourcePortID") or None
        if s not in by_id or t not in by_id:
            raise FlowError(f"edge {i} connects a node that does not exist ({s} -> {t})")
        if s == t:
            raise FlowError(f"edge {i}: a node cannot connect to itself")
        if by_id[t]["type"] == "start":
            raise FlowError("nothing can lead into the start node")
        if by_id[s]["type"] == "end":
            raise FlowError("an end node has no outgoing edges")
        named = ports(by_id[s])
        if named and port not in named:
            raise FlowError(f"edge {i}: node {s} branches; the edge must name one of its ports: {', '.join(named)}")
        if not named:
            port = None
        if (s, t, port) in seen_edges:
            continue
        seen_edges.add((s, t, port))
        out_edges.append({**e, "sourceNodeID": s, "targetNodeID": t, **({"sourcePortID": port} if port else {})})
    _check_acyclic(by_id, out_edges)
    return {**{k: v for k, v in doc.items() if k not in ("nodes", "edges")}, "name": name,
            "title": _text(doc.get("title"), "title", 200) or name,
            "description": _text(doc.get("description"), "description", 2000),
            "nodes": out_nodes, "edges": out_edges}


def _check_acyclic(by_id: dict, edges: list[dict]) -> None:
    out: dict[str, list[str]] = {n: [] for n in by_id}
    for e in edges:
        out[e["sourceNodeID"]].append(e["targetNodeID"])
    state: dict[str, int] = {}

    def visit(n: str) -> None:
        stack = [(n, iter(out[n]))]
        state[n] = 1
        while stack:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                state[node] = 2
                stack.pop()
            elif state.get(nxt) == 1:
                raise FlowError(f"the flow has a loop through {nxt}; flows run once from start to end")
            elif nxt not in state:
                state[nxt] = 1
                stack.append((nxt, iter(out[nxt])))

    for n in by_id:
        if n not in state:
            visit(n)


def has_types(doc: dict, *types: str) -> list[str]:
    return [n["id"] for n in doc["nodes"] if n["type"] in types]


def inputs_of(doc: dict) -> list[dict]:
    start = next(n for n in doc["nodes"] if n["type"] == "start")
    return start["data"].get("inputs", [])


# ---- templates and conditions -------------------------------------------------------------------------------
def render(text: str, scope: dict) -> str:
    """Fill {{inputs.x}} and {{node.field}} from the run's scope: {"inputs": {...}, node_id: {result, ...}}."""
    def one(m: re.Match) -> str:
        head, field = m.group(1), m.group(2)
        val = scope.get(head)
        if head == "inputs":
            return str((val or {}).get(field or "", "")) if field else ""
        if not isinstance(val, dict):
            return ""
        got = val.get(field or "result", "")
        return "" if got is None else str(got)
    return TEMPLATE_RE.sub(one, text or "")


def _num(v: str) -> float | None:
    try:
        return float(v.strip())
    except (ValueError, AttributeError):
        return None


def check(cond: dict, scope: dict) -> bool:
    """One condition rule, on rendered text. contains / equals ignore case and surrounding space."""
    left = render(cond.get("left", ""), scope)
    right = render(cond.get("right", ""), scope)
    op = cond.get("op", "contains")
    lo, ro = left.strip().lower(), right.strip().lower()
    if op == "contains":
        return ro in lo
    if op == "not_contains":
        return ro not in lo
    if op == "equals":
        return lo == ro
    if op == "not_equals":
        return lo != ro
    if op == "matches":
        return re.search(right, left, re.I | re.S) is not None
    if op == "empty":
        return not left.strip()
    if op == "not_empty":
        return bool(left.strip())
    a, b = _num(left), _num(right)
    if a is None or b is None:
        return False
    return a > b if op == "gt" else a < b
