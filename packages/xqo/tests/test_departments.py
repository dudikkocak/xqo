"""Departments: config, two-level broadcast, contact rules, resources, budgets, reload, dashboard API."""
import asyncio
import io
import json
import shutil
import zipfile
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from xqo.bus import HandoverError
from xqo.config import build, reload_departments
from xqo.departments import HQ, Budget, load_files, parse_only
from xqo.models import Task
from xqo.web import create_app

ROOT = Path(__file__).resolve().parent.parent


def setup(tmp_path, edit=None):
    """Copy agents.json + departments/ into tmp_path, optionally editing department dicts."""
    shutil.copy(ROOT / "agents.json", tmp_path / "agents.json")
    shutil.copytree(ROOT / "departments", tmp_path / "departments")
    if edit:
        for p in (tmp_path / "departments").glob("*.json"):
            d = json.loads(p.read_text())
            d = edit(d) or d
            p.write_text(json.dumps(d))
    return tmp_path / "agents.json"


def bus_for(tmp_path, cfg=None, **kw):
    cfg = cfg or setup(tmp_path)
    return build(cfg, tmp_path / "artifacts", str(tmp_path / "logs" / "e.jsonl"), mock=True, **kw)


def kinds(b, *names):
    return [e for e in b.history if e["kind"] in names]


# ------------------------------------------------------------------ config & loading
def test_load_and_validate(tmp_path):
    folder = tmp_path / "d"
    folder.mkdir()
    (folder / "ops.json").write_text(json.dumps({"agents": [{"name": "op", "skills": ["ops"]}]}))
    [d] = load_files(folder)
    assert d["name"] == "ops" and d["head"] == "op"  # name from the file, head defaults to first agent
    for bad, msg in [({"name": "Bad Name", "agents": [{"name": "x", "skills": []}]}, "invalid department name"),
                     ({"name": "x", "agents": []}, "no agents"),
                     ({"name": "x", "head": "ghost", "agents": [{"name": "y", "skills": []}]}, "head 'ghost'"),
                     ({"name": "hq", "agents": [{"name": "y", "skills": []}]}, "invalid department name")]:
        (folder / "bad.json").write_text(json.dumps(bad))
        with pytest.raises(ValueError, match=msg):
            load_files(folder)
    (folder / "bad.json").write_text("{nope")
    with pytest.raises(ValueError, match="invalid JSON"):
        load_files(folder)
    (folder / "bad.json").unlink()
    with pytest.raises(ValueError, match="unknown department"):
        load_files(folder, ["ops", "sales"])
    assert parse_only("eng, marketing") == ["eng", "marketing"] and parse_only("none") == [] and parse_only("all") is None


def test_build_assigns_departments_and_filters(tmp_path):
    b = bus_for(tmp_path)
    assert set(b.departments) == {"engineering", "marketing", "research"}
    assert b.agents["planner"].department == HQ and b.agents["coder"].department == "engineering"
    assert b.agents["eng_lead"].is_head and not b.agents["coder"].is_head
    assert b.agents["tester"].sandbox is not None and b.agents["tester"].sandbox.config.network is False
    (tmp_path / "x").mkdir()
    b2 = bus_for(tmp_path / "x", departments="engineering")
    assert set(b2.departments) == {"engineering"} and "writer" not in b2.agents
    (tmp_path / "y").mkdir()
    b3 = bus_for(tmp_path / "y", departments="none")
    assert b3.departments == {} and list(b3.agents) == ["planner"]


def test_duplicate_agent_names_rejected(tmp_path):
    cfg = setup(tmp_path, lambda d: {**d, "agents": d["agents"] + [{"name": "planner", "skills": ["x"]}]}
                if d["name"] == "research" else d)
    with pytest.raises(ValueError, match="unique"):
        bus_for(tmp_path, cfg)


def test_system_prompt_describes_structure(tmp_path):
    b = bus_for(tmp_path)
    s = b.agents["coder"]._system()
    assert "in the engineering department (head: eng_lead)" in s and "- tester" in s
    assert "research (head: research_lead)" in s and "marketing (head" not in s  # engineering can't reach marketing
    assert "you are its head" in b.agents["eng_lead"]._system()
    assert "headquarters (HQ)" in b.agents["planner"]._system()
    assert "publish_files" in [t["name"] for t in b.agents["coder"].tools()[0]]
    assert "publish_files" not in [t["name"] for t in b.agents["planner"].tools()[0]]


# ------------------------------------------------------------------ routing
def test_two_level_broadcast_through_head(tmp_path):
    b = bus_for(tmp_path)
    t = asyncio.run(b.submit("Build fib with run tests"))
    assert t.status == "done"
    bc = kinds(b, "broadcast")
    assert [(e["sender"], e["meta"]["scope"], e["meta"]["department"]) for e in bc[:3]] == [
        ("planner", "department", "hq"), ("planner", "heads", None), ("eng_lead", "department", "engineering")]
    offers = kinds(b, "offer")
    assert offers[0]["sender"] == "eng_lead" and offers[0]["meta"]["via"] == "head" and offers[0]["meta"]["member"] == "coder"
    hand = [(e["sender"], e["recipient"]) for e in kinds(b, "handover")]
    assert hand[:2] == [("planner", "eng_lead"), ("eng_lead", "coder")] and ("eng_lead", "tester") in hand
    assert {e["dept"] for e in kinds(b, "handover")} == {"hq", "engineering"}
    assert b.memory.notes(f"{t.id}.engineering")["last_assignment"]["author"] == "eng_lead"


def test_targeted_and_forbidden_lookups(tmp_path):
    b = bus_for(tmp_path)

    async def go():
        root = await b.create("x", "planner")
        offers = await b.broadcast_skill(root, "@research research", "find facts")
        assert [o["agent"] for o in offers] == ["research_lead"]
        assert await b.broadcast_skill(root, "python", "x", department="marketing") == []
        coder_task = b._child(root, "eng_lead", "x")
        coder_task = b._child(coder_task, "coder", "y")
        b.tasks[coder_task.id] = coder_task
        with pytest.raises(HandoverError, match="may not contact department 'marketing'"):
            await b.broadcast_skill(coder_task, "@marketing writing", "copy")
        with pytest.raises(HandoverError, match="unknown department"):
            await b.broadcast_skill(coder_task, "x", "y", department="legal")
        # untargeted lookups from engineering only escalate to departments it may contact
        await b.broadcast_skill(coder_task, "copywriting", "slogan")
        assert not [e for e in kinds(b, "offer") if e["sender"] == "mkt_lead"]
        with pytest.raises(HandoverError, match="may not contact"):
            await b.direct(coder_task, "writer", "hi")
        with pytest.raises(HandoverError, match="may not contact"):
            await b.direct(coder_task, "mkt_lead", "hi")
        analyst = b._child(root, "analyst", "z")
        b.tasks[analyst.id] = analyst
        with pytest.raises(HandoverError, match="send work for it to its head 'eng_lead'"):
            await b.direct(analyst, "coder", "hi")
        assert (await b.direct(analyst, "eng_lead", "Implement fib please")).startswith("Department result")
        assert "guard" in await b.direct(root, "reviewer", "Review it")  # HQ reaches anyone directly
    asyncio.run(go())


# ------------------------------------------------------------------ resources
def call(name, **inp):
    return NS(id="toolu_1", name=name, input=inp)


def test_department_workspace_notes_and_publish(tmp_path):
    b = bus_for(tmp_path)

    async def go():
        root = await b.create("x", "planner")
        t = b._child(root, "eng_lead", "x")
        b.tasks[t.id] = t
        lead = b.agents["eng_lead"]
        r = await lead._tool(t, call("write_file", path="draft/a.py", content="x = 1\n", scope="department"))
        assert "create draft/a.py" in r["content"]
        assert b.artifacts.list_files(root.id) == []  # private until published
        assert json.loads((await lead._tool(t, call("list_files", scope="department")))["content"])[0]["file"] == "draft/a.py"
        await lead._tool(t, call("note", key="k", value="v", scope="department"))
        assert "k" in b.memory.notes(f"{root.id}.engineering") and not b.memory.notes(root.id)
        r = await lead._tool(t, call("publish_files", files=["*"]))
        assert "published 1 file" in r["content"]
        assert b.artifacts.read_file(root.id, "draft/a.py") == "x = 1\n"
        assert b.artifacts.history(root.id, "draft/a.py")[0]["action"] == "publish"
        r = await b.agents["planner"]._tool(root, call("list_files", scope="department"))
        assert r["is_error"] and "headquarters has no department workspace" in r["content"]
        names = zipfile.ZipFile(io.BytesIO(b.artifacts.zip_task(root.id))).namelist()
        assert "workspace/draft/a.py" in names and "departments/engineering/draft/a.py" in names
    asyncio.run(go())
    with TestClient(create_app(b)) as c:
        arts = c.get("/api/artifacts").json()
        assert any(a["root_task_name"].endswith("· engineering department") for a in arts)


def test_mcp_servers_stay_in_their_department(tmp_path):
    def edit(d):
        if d["name"] == "research":
            d["mcp_servers"] = [{"name": "papers", "url": "https://example.com/mcp"}]
            d["agents"][1]["mcp"] = "all"
        if d["name"] == "engineering":
            d["agents"][1]["mcp"] = ["papers"]  # coder asks for research's server: not granted
    b = bus_for(tmp_path, setup(tmp_path, edit))
    assert [s["name"] for s in b.agents["analyst"].mcp_servers] == ["papers"]
    assert b.agents["coder"].mcp_servers == []


# ------------------------------------------------------------------ budgets
def test_budget_cost_and_caps():
    bus = NS(departments={"eng": NS(budget={"max_tokens": 100, "max_cost_usd": 1.0})})
    bg = Budget(bus)
    u = {"input_tokens": 1_000_000, "output_tokens": 0}
    assert bg.cost("claude-opus-5-5", u) == pytest.approx(4.0)
    assert bg.cost("unknown-model", u) == 0.0
    bg.add("r", "eng", "claude-opus-5-5", {"input_tokens": 50, "output_tokens": 10})
    assert bg.over("r", "eng") is None
    bg.add("r", "eng", "claude-opus-5-5", {"input_tokens": 50})
    assert "110 tokens of 100" in bg.over("r", "eng")
    bg.extensions[("r", "eng")] = 1
    assert bg.over("r", "eng") is None and bg.report("eng")["by_task"]["r"]["tokens"] == 110


def test_budget_stops_department_by_default(tmp_path):
    cfg = setup(tmp_path, lambda d: {**d, "budget": {"max_tokens": 1}} if d["name"] == "engineering" else d)
    b = bus_for(tmp_path, cfg)
    t = asyncio.run(b.submit("Build fib"))
    att = [e for e in kinds(b, "attention_requested") if e["meta"]["kind"] == "budget"]
    assert att and "engineering" in att[0]["content"]
    errs = [e["content"] for e in kinds(b, "error")]
    assert any("budget exhausted" in e for e in errs)
    assert t.status == "done"  # HQ carries on; only the department's work stopped


def test_budget_extend(tmp_path):
    cfg = setup(tmp_path, lambda d: {**d, "budget": {"max_tokens": 1}} if d["name"] == "engineering" else d)
    b = bus_for(tmp_path, cfg)

    async def go():
        job = asyncio.create_task(b.submit("Build fib"))
        for _ in range(500):
            p = [a for a in b.attention.pending() if a.kind == "budget"]
            if p:
                b.attention.resolve(p[0].id, "extend")
                break
            await asyncio.sleep(0.005)
        return await job
    t = asyncio.run(go())
    assert b.budget.extensions[(t.id, "engineering")] >= 1
    assert any(e["sender"] == "coder" and e["kind"] == "done" for e in b.history)


# ------------------------------------------------------------------ reload & API
def test_reload_adds_and_removes(tmp_path):
    cfg = setup(tmp_path)
    b = bus_for(tmp_path, cfg)
    folder = tmp_path / "departments"
    (folder / "legal.json").write_text(json.dumps({"name": "legal", "head": "counsel", "agents": [
        {"name": "counsel", "skills": ["contracts", "compliance"]}]}))
    (folder / "marketing.json").unlink()
    usage_before = b.agents["coder"].usage
    s = reload_departments(b)
    assert s["added"] == ["legal"] and s["removed"] == ["marketing"]
    assert "counsel" in b.agents and "writer" not in b.agents and b.agents["coder"].usage is usage_before
    assert kinds(b, "departments")[-1]["meta"]["added"] == ["legal"]
    assert reload_departments(b, "engineering")["departments"] == ["engineering"]
    (folder / "legal.json").write_text("{broken")
    with pytest.raises(ValueError):
        reload_departments(b, "all")
    assert set(b.departments) == {"engineering"}  # a failed reload changes nothing
    assert reload_departments(b)["departments"] == ["engineering"]  # and keeps the previous selection


def test_api(tmp_path):
    b = bus_for(tmp_path)
    asyncio.run(b.submit("Build fib"))
    with TestClient(create_app(b)) as c:
        d = {x["name"]: x for x in c.get("/api/departments").json()}
        assert list(d)[0] == "hq" and d["engineering"]["head"] == "eng_lead"
        assert {a["name"] for a in d["engineering"]["agents"]} == {"eng_lead", "coder", "reviewer", "tester"}
        assert d["engineering"]["usage"]["tokens"] > 0 and d["engineering"]["usage"]["cost"] > 0
        assert c.post("/api/departments/reload", json={"departments": "research"}).json()["departments"] == ["research"]
        assert c.post("/api/departments/reload", json={"departments": "nope"}).status_code == 400
        assert c.post("/api/departments/reload").status_code == 200
        ag = {a["name"]: a for a in c.get("/api/agents").json()}
        assert ag["analyst"]["department"] == "research" and ag["research_lead"]["head"] is True


def test_role_names_as_skills_find_department_members(tmp_path):
    b = bus_for(tmp_path)

    async def go():
        root = await b.create("x", "planner")
        lead = b._child(root, "eng_lead", "plan the chart plotter")
        b.tasks[lead.id] = lead
        for skill, who in [("coder", "coder"), ("tester", "tester"), ("reviewer", "reviewer")]:
            offers = await b.broadcast_skill(lead, skill, "do it")
            assert [o["agent"] for o in offers] == [who] and offers[0]["match"] == "exact"
        offers = await b.broadcast_skill(root, "researcher", "find facts")
        assert [o["agent"] for o in offers] == ["research_lead"]  # the research head takes it and assigns it

    asyncio.run(go())
