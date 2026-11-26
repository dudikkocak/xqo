"""Runs through the HQ's API with the scripted offline LLM: every agent step is a real xqo task."""
import asyncio
import json

from fastapi.testclient import TestClient

from xqo.web import create_app
from helpers import edge, flow, make_bus, node, wait_node, wait_run


def client(tmp_path, **kw):
    bus = make_bus(tmp_path, **kw)
    return bus, TestClient(create_app(bus))


def linear():
    return flow([node("s", "start", inputs=[{"name": "topic", "default": "sorting"}]),
                 node("a", "agent", agent="coder", prompt="Write code for {{inputs.topic}}"),
                 node("b", "agent", agent="writer", prompt="Explain:\n{{a.result}}"),
                 node("e", "end", output="RESULT {{b.result}}")],
                [edge("s", "a"), edge("a", "b"), edge("b", "e")])


def test_plugin_is_active_serves_editor_and_ui(tmp_path):
    bus, c = client(tmp_path)
    with c:
        row = {p["name"]: p for p in c.get("/api/plugins").json()["items"]}["flows"]
        assert row["state"] == "active" and row["ui"] == ["/plugins/flows/ui.js"]
        assert c.get("/plugins/flows/ui.js").status_code == 200
        assert c.get("/plugins/flows/editor.html").status_code == 200
        assert c.get("/api/flows").json() == []


def test_off_without_the_flows_key(tmp_path):
    from xqo.api import build
    cfg = tmp_path / "agents.json"
    cfg.write_text(json.dumps({"agents": [{"name": "coder", "skills": ["python"]}]}))
    bus = build(cfg, tmp_path / "a", str(tmp_path / "l" / "e.jsonl"), mock=True, departments="none")
    assert bus.plugins.states["flows"].state == "inactive"


def test_examples_are_written_once(tmp_path):
    bus, c = client(tmp_path, flows={"examples": True})
    with c:
        names = [f["name"] for f in c.get("/api/flows").json()]
        assert names == ["draft-review-approve", "hello"]
        c.delete("/api/flows/hello")
    with TestClient(create_app(make_bus(tmp_path, flows={"examples": True}))) as c:
        assert [f["name"] for f in c.get("/api/flows").json()] == ["draft-review-approve"]


def test_linear_run_passes_results_along_and_creates_tasks(tmp_path):
    bus, c = client(tmp_path)
    with c:
        assert c.put("/api/flows/demo", json=linear()).status_code == 200
        r = c.post("/api/flows/demo/run", json={"inputs": {"topic": "quicksort"}}).json()
        r = wait_run(c, r["id"])
        assert r["status"] == "done", r
        assert r["result"].startswith("RESULT ")
        a, b = r["nodes"]["a"], r["nodes"]["b"]
        assert a["status"] == b["status"] == "done" and a["task_id"] and b["task_id"]
        ta = bus.tasks[a["task_id"]]
        assert "quicksort" in ta.prompt and ta.assignee == "coder" and ta.unattended
        assert ta.result[:20] in bus.tasks[b["task_id"]].prompt  # a's result went into b's prompt
        kinds = [e["kind"] for e in bus.history if e["kind"].startswith("flow_")]
        assert kinds[0] == "flow_run" and kinds[-1] == "flow_run" and "flow_node" in kinds
        assert (tmp_path / "artifacts" / "flows" / "runs" / f"{r['id']}.json").is_file()
        assert c.get("/api/flows/runs?flow=demo").json()[0]["id"] == r["id"]


def test_bad_inputs_unknown_agents_and_unknown_flows(tmp_path):
    bus, c = client(tmp_path)
    with c:
        d = linear()
        d["nodes"][1]["data"]["agent"] = "ghost"
        saved = c.put("/api/flows/demo", json=d).json()
        assert saved["warnings"] == ["node a: unknown agent 'ghost'"]
        assert "unknown agent" in c.post("/api/flows/demo/run", json={}).json()["detail"]
        c.put("/api/flows/demo", json=linear())
        assert "unknown input" in c.post("/api/flows/demo/run", json={"inputs": {"nope": 1}}).json()["detail"]
        assert c.post("/api/flows/missing/run", json={}).status_code == 404
        assert c.put("/api/flows/runs", json=linear()).status_code == 422


def branching():
    return flow([node("s", "start", inputs=[{"name": "mode", "default": "a"}]),
                 node("c", "condition", conditions=[{"key": "is_a", "left": "{{inputs.mode}}", "op": "equals", "right": "a"}]),
                 node("a", "agent", agent="coder", prompt="branch a"),
                 node("b", "agent", agent="writer", prompt="branch b"),
                 node("j", "end", output="{{c.result}}|{{a.status}}|{{b.status}}")],
                [edge("s", "c"), edge("c", "a", "is_a"), edge("c", "b", "else"), edge("a", "j"), edge("b", "j")])


def test_condition_takes_one_branch_and_the_join_waits_only_for_it(tmp_path):
    bus, c = client(tmp_path)
    with c:
        c.put("/api/flows/br", json=branching())
        r = wait_run(c, c.post("/api/flows/br/run", json={"inputs": {"mode": "a"}}).json()["id"])
        assert r["status"] == "done" and r["result"] == "is_a|done|"
        assert r["nodes"]["b"]["status"] == "skipped" and r["nodes"]["c"]["port"] == "is_a"
        r = wait_run(c, c.post("/api/flows/br/run", json={"inputs": {"mode": "zzz"}}).json()["id"])
        assert r["result"] == "else||done" and r["nodes"]["a"]["status"] == "skipped"


def test_parallel_branches_both_run_and_join(tmp_path):
    bus, c = client(tmp_path)
    with c:
        d = flow([node("s", "start"), node("a", "agent", agent="coder", prompt="one"),
                  node("b", "agent", agent="writer", prompt="two"), node("e", "end", output="{{a.status}}+{{b.status}}")],
                 [edge("s", "a"), edge("s", "b"), edge("a", "e"), edge("b", "e")])
        c.put("/api/flows/par", json=d)
        r = wait_run(c, c.post("/api/flows/par/run", json={}).json()["id"])
        assert r["status"] == "done" and r["result"] == "done+done"


def approval_flow():
    return flow([node("s", "start"), node("ok", "approval", question="Ship it?"),
                 node("yes", "end", output="shipped"), node("no", "end", output="held")],
                [edge("s", "ok"), edge("ok", "yes", "approve"), edge("ok", "no", "reject")])


def test_approval_waits_for_the_user(tmp_path):
    bus, c = client(tmp_path)
    with c:
        c.put("/api/flows/ap", json=approval_flow())
        rid = c.post("/api/flows/ap/run", json={}).json()["id"]
        r = wait_node(c, rid, "ok", "waiting")
        assert r["status"] == "waiting" and r["nodes"]["ok"]["question"] == "Ship it?"
        assert c.get("/api/flows/approvals").json()[0]["node"] == "ok"
        assert c.post(f"/api/flows/runs/{rid}/approve", json={"node": "nope"}).status_code == 404
        assert c.post(f"/api/flows/runs/{rid}/approve", json={"node": "ok", "approve": True}).json()["answer"] == "approve"
        r = wait_run(c, rid)
        assert r["status"] == "done" and r["result"] == "shipped" and r["nodes"]["no"]["status"] == "skipped"
        rid = c.post("/api/flows/ap/run", json={}).json()["id"]
        wait_node(c, rid, "ok", "waiting")
        c.post(f"/api/flows/runs/{rid}/approve", json={"node": "ok", "approve": False})
        assert wait_run(c, rid)["result"] == "held"


def test_approval_timeout_takes_the_default(tmp_path):
    bus, c = client(tmp_path)
    with c:
        d = approval_flow()
        d["nodes"][1]["data"].update(timeout=0.2, default="approve")
        c.put("/api/flows/ap", json=d)
        r = wait_run(c, c.post("/api/flows/ap/run", json={}).json()["id"])
        assert r["result"] == "shipped" and r["nodes"]["ok"]["answered_by"] == "timeout"


def test_cancel_stops_a_waiting_run(tmp_path):
    bus, c = client(tmp_path)
    with c:
        c.put("/api/flows/ap", json=approval_flow())
        rid = c.post("/api/flows/ap/run", json={}).json()["id"]
        wait_node(c, rid, "ok", "waiting")
        assert c.post(f"/api/flows/runs/{rid}/cancel").json()["status"] == "cancelled"
        r = c.get(f"/api/flows/runs/{rid}").json()
        assert r["nodes"]["ok"]["status"] == "cancelled" and r["status"] == "cancelled"
        assert c.post(f"/api/flows/runs/{rid}/cancel").status_code == 409


def test_a_failed_step_fails_the_run_unless_continue_on_error(tmp_path):
    bus, c = client(tmp_path)

    async def boom(*a, **k):
        raise RuntimeError("model down")

    with c:
        bus.agents["coder"].handle = boom
        d = flow([node("s", "start"), node("a", "agent", agent="coder", prompt="x"), node("e", "end", output="after")],
                 [edge("s", "a"), edge("a", "e")])
        c.put("/api/flows/f", json=d)
        r = wait_run(c, c.post("/api/flows/f/run", json={}).json()["id"])
        assert r["status"] == "error" and r["nodes"]["a"]["status"] == "error" and r["nodes"]["e"]["status"] == "skipped"
        d["nodes"][1]["data"]["continue_on_error"] = True
        c.put("/api/flows/f", json=d)
        r = wait_run(c, c.post("/api/flows/f/run", json={}).json()["id"])
        assert r["status"] == "done" and r["result"] == "after" and r["nodes"]["a"]["status"] == "error"


def test_restart_marks_running_runs_interrupted(tmp_path):
    bus, c = client(tmp_path)
    with c:
        c.put("/api/flows/ap", json=approval_flow())
        rid = c.post("/api/flows/ap/run", json={}).json()["id"]
        wait_node(c, rid, "ok", "waiting")
    with TestClient(create_app(make_bus(tmp_path))) as c:
        r = c.get(f"/api/flows/runs/{rid}").json()
        assert r["status"] == "interrupted"


def test_run_flow_tool(tmp_path):
    bus = make_bus(tmp_path, flows={"agents_may_run": ["coder"]})
    svc = bus.plugins.service("flows")
    coder, writer = bus.agents["coder"], bus.agents["writer"]
    assert [t["name"] for t in bus.plugins.tool_defs(coder)] .count("run_flow") == 1
    assert "run_flow" not in [t["name"] for t in bus.plugins.tool_defs(writer)]

    async def go():
        await svc.start()
        svc.save("demo", linear())
        assert "demo" in svc.describe_for_agent()
        out = await svc.tool_run(None, coder, {"name": "demo", "inputs": {"topic": "heaps"}})
        assert out.startswith("RESULT ")
        assert "could not start" in await svc.tool_run(None, coder, {"name": "nope"})
        inner = bus.tasks[next(iter(svc.steps.task_ids))]
        assert "not available inside a step" in await svc.tool_run(inner, coder, {"name": "demo"})
        await svc.stop()

    asyncio.run(go())
