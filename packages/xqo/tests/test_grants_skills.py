"""Grants (default_mcp, "+name", tool filters, tool policy), skill packs and MCP hot reload."""
import asyncio
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from xqo.config import build, reload_departments, sync_mcp
from xqo.grants import ToolPolicy, resolve
from xqo.mcp_hub import MCPHub, remote_params
from xqo.models import Task
from xqo.skills import load_pack, split_frontmatter
from xqo.web import create_app

ROOT = Path(__file__).resolve().parent.parent
ECHO = {"name": "echo", "command": sys.executable, "args": [str(ROOT / "tests" / "mcp_echo_server.py")]}


def setup(tmp_path, cfg_edit=None, dept_edit=None):
    cfg = json.loads((ROOT / "agents.json").read_text())
    cfg.pop("default_provider", None)
    if cfg_edit:
        cfg_edit(cfg)
    (tmp_path / "agents.json").write_text(json.dumps(cfg))
    shutil.copytree(ROOT / "departments", tmp_path / "departments")
    if dept_edit:
        for p in (tmp_path / "departments").glob("*.json"):
            d = json.loads(p.read_text())
            dept_edit(d)
            p.write_text(json.dumps(d))
    return tmp_path / "agents.json"


def bus_for(tmp_path, cfg_path):
    return build(cfg_path, tmp_path / "artifacts", str(tmp_path / "logs" / "e.jsonl"), mock=True)


def skill(folder: Path, name: str, desc: str = "Does a thing.", body: str = "Step 1.", files: dict | None = None):
    d = folder / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n{body}\n")
    for rel, text in (files or {}).items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text)
    return d


# ------------------------------------------------------------------ inheritance rules
def test_resolve_replaces_and_adds():
    cfg = {"default_mcp": ["docs"]}
    assert resolve("mcp", "default_mcp", {}, None, cfg, "a") == ({"docs": None}, "config")
    assert resolve("mcp", "default_mcp", {}, {"default_mcp": ["+gh"]}, cfg, "a")[0] == {"docs": None, "gh": None}
    assert resolve("mcp", "default_mcp", {"mcp": ["fs"]}, {"default_mcp": ["+gh"]}, cfg, "a") == ({"fs": None}, "agent")
    assert resolve("mcp", "default_mcp", {"mcp": ["+fs"]}, None, cfg, "a")[0] == {"docs": None, "fs": None}
    assert resolve("mcp", "default_mcp", {"mcp": []}, None, cfg, "a")[0] == {}  # an empty list removes them all
    assert resolve("mcp", "default_mcp", {"mcp": {"gh": ["search_*"], "+fs": "*"}}, None, cfg, "a")[0] == \
        {"gh": ["search_*"], "fs": None}
    assert resolve("mcp", "default_mcp", {"mcp": "all"}, None, {}, "a")[0] == {"all": None}
    with pytest.raises(ValueError, match="must be a list"):
        resolve("mcp", "default_mcp", {"mcp": "docs"}, None, {}, "a")


def test_tool_policy():
    p = ToolPolicy.resolve({"tools": {"deny": ["+mcp__gh__delete_*"]}}, {"default_tools": {"deny": ["run_code"]}}, {}, "a")
    assert p.deny == ["run_code", "mcp__gh__delete_*"] and p.source == "agent"
    assert not p.allows("run_code") and not p.allows("mcp__gh__delete_repo") and p.allows("mcp__gh__search")
    only = ToolPolicy(allow=["handover_task", "mcp__docs__*"])
    assert only.allows("handover_task") and not only.allows("note")
    assert only.allows_server("docs") and not only.allows_server("gh")
    assert not ToolPolicy(deny=["mcp__docs__*"]).allows_server("docs")
    assert ToolPolicy(deny=["mcp__docs__drop"]).allows_server("docs")  # one tool: the server stays
    with pytest.raises(ValueError, match="allow"):
        ToolPolicy.resolve({"tools": {"block": []}}, None, {}, "a")


def test_remote_params_per_agent_filters():
    srv = {"name": "docs", "url": "https://x/mcp", "allowed_tools": ["search", "fetch"]}
    _, [ts] = remote_params([srv], {"docs": ["search", "write"]})
    assert ts["configs"] == {"search": {"enabled": True}}  # the agent can't widen the server's own allowlist
    _, [ts] = remote_params([{"name": "d", "url": "u"}], {"d": None}, {"d": {"drop"}})
    assert ts == {"type": "mcp_toolset", "mcp_server_name": "d", "configs": {"drop": {"enabled": False}}}


# ------------------------------------------------------------------ config wiring
def test_default_mcp_for_everyone_and_per_department(tmp_path):
    def cfg_edit(c):
        c["mcp_servers"] = [{"name": "docs", "url": "https://x/mcp"}]
        c["default_mcp"] = ["docs"]

    def dept_edit(d):
        if d["name"] == "engineering":
            d["mcp_servers"] = [{"name": "gh", "url": "https://gh/mcp"}]
            d["default_mcp"] = ["+gh"]
            for a in d["agents"]:
                if a["name"] == "coder":
                    a["mcp"] = {"gh": ["search_code"]}
    b = bus_for(tmp_path, setup(tmp_path, cfg_edit, dept_edit))
    assert [s["name"] for s in b.agents["planner"].mcp_servers] == ["docs"]
    assert b.agents["planner"].mcp_source == "config"
    assert sorted(s["name"] for s in b.agents["eng_lead"].mcp_servers) == ["docs", "gh"]
    coder = b.agents["coder"]
    assert coder.mcp_filters == {"gh": ["search_code"]} and coder.mcp_source == "agent"
    other = next(a for a in b.agents.values() if a.department == "marketing")
    assert [s["name"] for s in other.mcp_servers] == ["docs"]  # gh belongs to engineering only
    coder.provider_type = "anthropic"  # the MCP connector path (claude-sdk agents get Claude Code http servers)
    tools, servers = coder.tools()
    ts = [t for t in tools if t.get("type") == "mcp_toolset"]
    assert servers[0]["name"] == "gh" and ts[0]["configs"] == {"search_code": {"enabled": True}}


def test_unavailable_server_warns_and_patterns_need_local(tmp_path):
    def dept_edit(d):
        if d["name"] == "engineering":
            d["mcp_servers"] = [{"name": "gh", "url": "https://gh/mcp"}]
    b = bus_for(tmp_path / "a", setup(_mk(tmp_path / "a"), lambda c: c["agents"][0].update(mcp=["gh"]), dept_edit))
    assert b.agents["planner"].mcp_servers == []
    assert any("'gh' is not available" in w for w in b.config_warnings)

    def bad(c):
        c["mcp_servers"] = [{"name": "docs", "url": "https://x/mcp"}]
        c["agents"][0]["mcp"] = {"docs": ["search_*"]}
    with pytest.raises(ValueError, match="exact tool names"):
        bus_for(tmp_path / "b", setup(_mk(tmp_path / "b"), bad))


def _mk(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def test_tool_policy_filters_offered_tools_and_refuses_calls(tmp_path):
    def cfg_edit(c):
        c["default_tools"] = {"deny": ["note"]}
        c["agents"][0]["tools"] = {"deny": ["+read_notes"]}
    b = bus_for(tmp_path, setup(tmp_path, cfg_edit))
    planner = b.agents["planner"]
    names = {t["name"] for t in planner.tools()[0] if "name" in t}
    assert "note" not in names and "read_notes" not in names and "handover_task" in names
    eng = b.agents["eng_lead"]
    assert "read_notes" in {t.get("name") for t in eng.tools()[0]}  # the agent's own "+" adds only for itself
    t = Task(prompt="x", assignee="planner", chain=["planner"])
    res = asyncio.run(planner._tool(t, NS(id="c1", name="note", input={"key": "k", "value": "v"})))
    assert res["is_error"] and "tool policy" in res["content"]


# ------------------------------------------------------------------ skill packs
def test_frontmatter_reader():
    meta, body = split_frontmatter("---\nname: x\ndescription: >\n  long\n  text\nother: 'q'\n---\nBody")
    assert meta == {"name": "x", "description": "long text", "other": "q"} and body.strip() == "Body"
    assert split_frontmatter("no front matter") == ({}, "no front matter")


def test_pack_validation(tmp_path):
    d = tmp_path / "Bad Name"
    d.mkdir()
    (d / "SKILL.md").write_text("---\ndescription: x\n---\n")
    with pytest.raises(ValueError, match="lowercase"):
        load_pack(d)
    (d / "SKILL.md").write_text("---\nname: ok\n---\n")
    with pytest.raises(ValueError, match="description is required"):
        load_pack(d)


def test_skill_packs_granted_listed_and_loaded(tmp_path):
    root = tmp_path / "hq"
    root.mkdir()
    skill(root / "skills", "release-notes", "Write release notes from a changelog.", "Use headings.",
          {"reference/style.md": "Short lines.", "scripts/count.py": "print(42)"})
    skill(root / "skills", "brand-voice", "Our brand voice.")
    eng_skills = root / "departments" / "eng-skills"

    def cfg_edit(c):
        c["default_skill_packs"] = ["brand-voice"]
        c["agents"][0]["skill_packs"] = ["+release-notes"]

    def dept_edit(d):
        if d["name"] == "engineering":
            d["skills_dir"] = "eng-skills"
            d["default_skill_packs"] = "all"
    cfgp = setup(root, cfg_edit, dept_edit)
    skill(eng_skills, "code-style", "House code style.")
    b = bus_for(root, cfgp)
    planner = b.agents["planner"]
    assert [p.name for p in planner.skill_packs] == ["brand-voice", "release-notes"]
    assert sorted(p.name for p in b.agents["coder"].skill_packs) == ["brand-voice", "code-style", "release-notes"]
    mk = next(a for a in b.agents.values() if a.department == "marketing")
    assert [p.name for p in mk.skill_packs] == ["brand-voice"]  # code-style is engineering's own
    assert "release-notes: Write release notes" in planner._system()
    assert {"load_skill", "read_skill_file"} <= {t.get("name") for t in planner.tools()[0]}

    t = Task(prompt="x", assignee="planner", chain=["planner"])
    out = asyncio.run(planner._tool(t, NS(id="c", name="load_skill", input={"name": "release-notes"})))["content"]
    assert "Use headings." in out and "reference/style.md" in out and "scripts/count.py" in out
    out = asyncio.run(planner._tool(t, NS(id="c", name="read_skill_file",
                                          input={"name": "release-notes", "path": "reference/style.md"})))
    assert out["content"] == "Short lines."
    bad = asyncio.run(planner._tool(t, NS(id="c", name="read_skill_file",
                                          input={"name": "release-notes", "path": "../brand-voice/SKILL.md"})))
    assert bad["is_error"]
    nope = asyncio.run(planner._tool(t, NS(id="c", name="load_skill", input={"name": "code-style"})))
    assert nope["is_error"] and "no skill pack" in nope["content"]

    with TestClient(create_app(b)) as c:
        assert {s["name"] for s in c.get("/api/config").json()["skills"]} == {"release-notes", "brand-voice", "code-style"}
        assert c.get("/api/agents").json()  # describe() carries the new fields
    assert b.agents["planner"].describe()["skill_packs"] == ["brand-voice", "release-notes"]


def test_skill_scripts_reach_run_code(tmp_path):
    from xqo.sandbox import Sandbox, SandboxConfig
    pack = skill(tmp_path / "skills", "count", files={"scripts/count.py": "print(6 * 7)"})
    ws = tmp_path / "ws"
    ws.mkdir()
    sb = Sandbox(SandboxConfig.from_dict({"backend": "local", "venv": False}))
    r = asyncio.run(sb.run(ws, f"{sys.executable} .skills/count/scripts/count.py", extra={"count": pack}))
    assert r["exit_code"] == 0 and r["stdout"].strip() == "42"
    assert not (ws / ".skills").exists()  # only the run copy gets them


def test_duplicate_pack_names_fail(tmp_path):
    root = tmp_path / "hq"
    root.mkdir()
    skill(root / "skills", "same")
    cfgp = setup(root, None, lambda d: d.update(skills_dir="s") if d["name"] == "engineering" else None)
    skill(root / "departments" / "s", "same")
    with pytest.raises(ValueError, match="exists twice"):
        bus_for(root, cfgp)


def test_reload_picks_up_new_skills(tmp_path):
    cfgp = setup(tmp_path, lambda c: c.update(default_skill_packs="all"))
    b = bus_for(tmp_path, cfgp)
    assert b.agents["planner"].skill_packs == []
    skill(tmp_path / "skills", "fresh")
    summary = reload_departments(b)
    assert summary["skills"] == ["fresh"] and [p.name for p in b.agents["planner"].skill_packs] == ["fresh"]


# ------------------------------------------------------------------ local MCP: filters and hot reload
def test_local_mcp_filter_and_hot_reload(tmp_path):
    pytest.importorskip("mcp")

    async def scenario():
        hub = MCPHub([])
        await hub.start()
        try:
            assert await hub.sync([ECHO]) == {"added": ["echo"], "removed": [], "restarted": []}
            assert not hub.errors, hub.errors
            assert {d["name"] for d in hub.tool_defs({"echo": ["ad*"]})} == {"mcp__echo__add"}
            changed = {**ECHO, "allowed_tools": ["echo"]}
            assert (await hub.sync([changed]))["restarted"] == ["echo"]
            assert set(hub.tools) == {"mcp__echo__echo"}
            assert await hub.call("mcp__echo__echo", {"text": "a"}) == ("A", False)
            assert (await hub.sync([]))["removed"] == ["echo"]
            assert hub.tools == {} and hub.status() == []
        finally:
            await hub.stop()

    asyncio.run(scenario())


def test_reload_endpoint_syncs_local_mcp(tmp_path):
    pytest.importorskip("mcp")
    cfgp = setup(tmp_path)
    b = bus_for(tmp_path, cfgp)
    with TestClient(create_app(b)) as c:
        cfg = json.loads(cfgp.read_text())
        cfg["mcp_servers"] = [ECHO]
        cfg["agents"][0]["mcp"] = {"echo": ["add"]}
        cfgp.write_text(json.dumps(cfg))
        r = c.post("/api/departments/reload").json()
        assert r["mcp"]["added"] == ["echo"]
        planner = b.agents["planner"]
        names = {t.get("name") for t in planner.tools()[0]}
        assert "mcp__echo__add" in names and "mcp__echo__echo" not in names
        assert planner.may_use_mcp("mcp__echo__add") and not planner.may_use_mcp("mcp__echo__echo")
        t = Task(prompt="x", assignee="planner", chain=["planner"])
        res = asyncio.run(planner._tool(t, NS(id="c", name="mcp__echo__echo", input={"text": "x"})))
        assert res["is_error"]  # not granted: refused even when the model names it
