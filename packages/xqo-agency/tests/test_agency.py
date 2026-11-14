"""agency-agents importer: add, sync, update (changed / renamed / deleted upstream), remove, overlay kept."""
import asyncio
import json
import shutil
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from xqo_agency import importer as agency
from xqo.config import PACKAGED_CONFIG, build

HQ = agency.resolve_dir is not None
ROOT = Path(__file__).resolve().parent.parent


def agent_md(name: str, body: str = "", emoji: str = "🔧") -> str:
    return f"---\nname: {name}\ndescription: >\n  Does {name}\n  things.\nemoji: {emoji}\n---\n\n# {name}\n\nYou are **{name}**.\n{body}"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def upstream(tmp_path):
    up = tmp_path / "upstream"
    for path, name in {"engineering/engineering-code-reviewer.md": "Code Reviewer",
                       "engineering/engineering-sre.md": "SRE",
                       "testing/testing-evidence-collector.md": "Evidence Collector",
                       "game-development/unity/unity-architect.md": "Unity Architect"}.items():
        (up / path).parent.mkdir(parents=True, exist_ok=True)
        (up / path).write_text(agent_md(name))
    (up / "integrations/claude-code").mkdir(parents=True)
    (up / "integrations/claude-code/README.md").write_text(agent_md("Not An Agent"))
    (up / "README.md").write_text("# readme\n")
    (up / "LICENSE").write_text("MIT License\n\nCopyright (c) 2025 AgentLand Contributors\n")
    (up / "divisions.json").write_text(json.dumps({"divisions": {
        "engineering": {"label": "Engineering"}, "testing": {"label": "Testing"},
        "game-development": {"label": "Game Development"}}}))
    git(up, "init", "-q")
    git(up, "add", "-A")
    git(up, "commit", "-qm", "one")
    return up


@pytest.fixture
def home(tmp_path):
    cfg = tmp_path / "home" / "agents.json"
    cfg.parent.mkdir()
    shutil.copy(PACKAGED_CONFIG, cfg)
    return cfg


def run(cfg, cmd, upstream, **kw):
    ns = {"config": str(cfg), "agency_cmd": cmd, "source": str(upstream), "reload": False,
          "url": "http://127.0.0.1:1", "ref": None, "dry_run": False, "check": False, "prune": False,
          "items": [], "department": None, "skills": None, "name": None,
          "division": None, "search": None, "new": False}
    agency.main(Namespace(**{**ns, **kw}))


def all_agents(cfg: Path) -> dict:
    out = {a["name"]: ("hq", a) for a in json.loads(cfg.read_text())["agents"]}
    for p in sorted((cfg.parent / "departments").glob("*.json")):
        d = json.loads(p.read_text())
        out.update({a["name"]: (d["name"], a) for a in d["agents"]})
    return out


def test_parse_agent():
    meta, body = agency.parse_agent(agent_md("Code Reviewer"))
    assert meta == {"name": "Code Reviewer", "description": "Does Code Reviewer things.", "emoji": "🔧"}
    assert body.startswith("# Code Reviewer")
    assert agency.parse_agent("# no frontmatter") is None
    assert agency.slug("Code Reviewer") == "code_reviewer" and agency.slug("3D Artist") == "a_3d_artist"


def test_reads_only_agents(upstream):
    up = agency.fetch("", "main", str(upstream))
    assert sorted(up.files) == ["engineering/engineering-code-reviewer.md", "engineering/engineering-sre.md",
                                "game-development/unity/unity-architect.md", "testing/testing-evidence-collector.md"]
    assert up.commit == git(upstream, "rev-parse", "HEAD") and up.license.startswith("MIT")


def test_add_update_remove(home, upstream, capsys):
    base = home.parent
    run(home, "add", upstream, items=["engineering/engineering-code-reviewer.md"], skills="code review,security",
        department="hq")
    run(home, "add", upstream, items=["testing"], department="engineering" if HQ else None)
    agents = all_agents(home)
    cr = agents["code_reviewer"][1]
    assert agents["code_reviewer"][0] == "hq"
    assert cr["skills"] == ["code review", "security"] and cr["avatar"] == "🔧"
    assert cr["system"].startswith("# Code Reviewer") and cr["system"].endswith(agency.ADAPTER)
    assert cr["_agency"]["path"] == "engineering/engineering-code-reviewer.md"
    assert agents["evidence_collector"][0] == ("engineering" if HQ else "hq")
    assert "planner" in agents  # hand-written agents stay
    assert (base / agency.LICENSE).read_text().startswith("MIT")
    lock = json.loads((base / agency.LOCK).read_text())
    assert lock["commit"] == git(upstream, "rev-parse", "HEAD") and len(lock["files"]) == 2

    # the user tunes the overlay; sync keeps it
    ov = json.loads((base / agency.OVERLAY).read_text())
    ov["agents"]["testing/testing-evidence-collector.md"]["can_run_code"] = True
    (base / agency.OVERLAY).write_text(json.dumps(ov))
    run(home, "sync", upstream)
    assert all_agents(home)["evidence_collector"][1]["can_run_code"] is True

    # upstream moves on: one prompt changes, one file is renamed, a new agent appears
    (upstream / "engineering/engineering-code-reviewer.md").write_text(agent_md("Code Reviewer", "New rule.\n"))
    git(upstream, "mv", "testing/testing-evidence-collector.md", "testing/testing-evidence.md")
    (upstream / "testing/testing-new.md").write_text(agent_md("New Tester"))
    git(upstream, "add", "-A")
    git(upstream, "commit", "-qm", "two")
    capsys.readouterr()
    with pytest.raises(SystemExit) as e:
        run(home, "update", upstream, check=True)
    assert e.value.code == 1
    out = capsys.readouterr().out
    assert "changed   engineering/engineering-code-reviewer.md  code_reviewer  +1 -0" in out
    assert "renamed   testing/testing-evidence-collector.md -> testing/testing-evidence.md" in out
    assert "1 new agent(s) upstream" in out
    assert json.loads((base / agency.LOCK).read_text())["commit"] == lock["commit"]  # --check writes nothing

    run(home, "update", upstream)
    agents = all_agents(home)
    assert "New rule." in agents["code_reviewer"][1]["system"]
    assert agents["code_reviewer"][1]["skills"] == ["code review", "security"]
    assert agents["evidence_collector"][1]["can_run_code"] is True
    ov = json.loads((base / agency.OVERLAY).read_text())
    assert "testing/testing-evidence.md" in ov["agents"] and "testing/testing-evidence-collector.md" not in ov["agents"]
    capsys.readouterr()
    run(home, "list", upstream, new=True)
    assert capsys.readouterr().out == "  testing/testing-new.md  New Tester - Does New Tester things.\n"

    # deleted upstream: kept and flagged, dropped only with --prune
    git(upstream, "rm", "-q", "engineering/engineering-code-reviewer.md")
    git(upstream, "commit", "-qm", "three")
    run(home, "update", upstream)
    assert "missing   engineering/engineering-code-reviewer.md" in capsys.readouterr().out
    assert "code_reviewer" in all_agents(home)
    run(home, "update", upstream, prune=True)
    assert "code_reviewer" not in all_agents(home)

    run(home, "remove", upstream, items=["evidence_collector"])
    agents = all_agents(home)
    assert not [a for _, a in agents.values() if "_agency" in a]
    assert {n for n, (d, _) in agents.items() if d == "hq"} == {
        a["name"] for a in json.loads(PACKAGED_CONFIG.read_text())["agents"]}


@pytest.mark.skipif(not HQ, reason="departments are hq-only")
def test_new_department_with_head(home, upstream, tmp_path):
    run(home, "add", upstream, items=["game-development"])
    d = json.loads((home.parent / "departments/game-development.json").read_text())
    assert d["head"] == "game-development_lead" and d["agents"][0]["name"] == "game-development_lead"
    assert [a["name"] for a in d["agents"][1:]] == ["unity_architect"]
    assert {"engineering.json", "marketing.json", "research.json"} <= {
        p.name for p in (home.parent / "departments").glob("*.json")}  # the packaged examples were copied first
    bus = build(home, tmp_path / "art", str(tmp_path / "ev.jsonl"), mock=True)
    assert bus.departments["game-development"].head == "game-development_lead"
    task = asyncio.run(bus.submit("Build fib"))
    assert task.status == "done"

    # department settings from the overlay; removing its last agent deletes the generated file
    ov = json.loads((home.parent / agency.OVERLAY).read_text())
    ov["departments"] = {"game-development": {"head": "unity_architect", "budget": {"max_cost_usd": 3}}}
    (home.parent / agency.OVERLAY).write_text(json.dumps(ov))
    run(home, "sync", upstream)
    d = json.loads((home.parent / "departments/game-development.json").read_text())
    assert d["head"] == "unity_architect" and d["budget"] == {"max_cost_usd": 3} and len(d["agents"]) == 1
    run(home, "remove", upstream, items=["game-development"])
    assert not (home.parent / "departments/game-development.json").exists()


def test_name_clash_and_bad_input_change_nothing(home, upstream):
    before = home.read_text()
    with pytest.raises(SystemExit, match="used twice"):
        run(home, "add", upstream, items=["engineering/engineering-sre.md"], name="planner", department="hq")
    with pytest.raises(SystemExit, match="matches no agent"):
        run(home, "add", upstream, items=["nope"])
    assert home.read_text() == before


def test_broken_result_is_rolled_back(home, upstream, monkeypatch):
    run(home, "add", upstream, items=["engineering/engineering-sre.md"], department="hq")
    before = {p: p.read_text() for p in home.parent.rglob("*.json")}
    ov = json.loads((home.parent / agency.OVERLAY).read_text())
    ov["agents"]["engineering/engineering-sre.md"]["effort"] = "bogus"
    (home.parent / agency.OVERLAY).write_text(json.dumps(ov))
    with pytest.raises(SystemExit):
        run(home, "sync", upstream)
    assert json.loads(home.read_text()) == json.loads(before[home])
