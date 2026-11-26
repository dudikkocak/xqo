import json
import time

from xqo.api import build

AGENTS = {"agents": [{"name": "coder", "skills": ["python"]}, {"name": "writer", "skills": ["python"]}]}


def make_bus(tmp_path, flows: dict | None = None, extra: dict | None = None):
    cfg = tmp_path / "agents.json"
    cfg.write_text(json.dumps({**AGENTS, "flows": {"examples": False, **(flows or {})}, **(extra or {})}))
    return build(cfg, tmp_path / "artifacts", str(tmp_path / "logs" / "events.jsonl"), mock=True, departments="none")


def node(nid, type_, **data):
    return {"id": nid, "type": type_, "meta": {"position": {"x": 0, "y": 0}}, "data": data}


def edge(s, t, port=None):
    return {"sourceNodeID": s, "targetNodeID": t, **({"sourcePortID": port} if port else {})}


def flow(nodes, edges, **kw):
    return {"title": kw.pop("title", "test"), "nodes": nodes, "edges": edges, **kw}


def wait_run(c, run_id, until=("done", "error", "cancelled", "interrupted"), timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = c.get(f"/api/flows/runs/{run_id}").json()
        if r["status"] in until:
            return r
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} still {r['status']}: {r}")


def wait_node(c, run_id, nid, status, timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = c.get(f"/api/flows/runs/{run_id}").json()
        if r["nodes"].get(nid, {}).get("status") == status:
            return r
        time.sleep(0.05)
    raise AssertionError(f"node {nid} of {run_id} never became {status}: {r['nodes'].get(nid)}")
