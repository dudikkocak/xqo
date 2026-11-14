"""xqo plugin system: discovery, disabling, failure isolation, strict mode, requirements, the hook dispatcher."""
import asyncio
import json
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from xqo import plugin as plugin_mod
from xqo.config import build
from xqo.models import Task
from xqo.plugin import Plugin, PluginManager
from xqo.web import create_app

AGENTS = [{"name": "planner", "skills": ["planning"]}, {"name": "coder", "skills": ["python"]}]


def make(tmp_path, monkeypatch=None, **cfg):
    p = tmp_path / "agents.json"
    p.write_text(json.dumps({"agents": AGENTS, **cfg}))
    return build(p, tmp_path / "artifacts", str(tmp_path / "events.jsonl"), mock=True, departments="none")


def test_builtins_office_active_and_the_rest_missing(tmp_path):
    bus = make(tmp_path)
    st = bus.plugins.health()
    assert st["office"] == "active" and bus.office
    for n in ("schedules", "channels", "feeds", "peering", "toolchains", "docker", "agency", "postgres"):
        assert st[n].startswith("missing"), (n, st[n])
    assert not any("plugin" in w for w in bus.config_warnings)  # not configured: missing, but nothing to say


def test_disable_removes_office_routes(tmp_path):
    bus = make(tmp_path, plugins={"disable": ["office", "schedules", "docker"]})
    assert bus.plugins.health()["office"] == "disabled" and bus.office is None
    assert not any("no plugin named" in w for w in bus.config_warnings)  # left-out plugins may be named
    with TestClient(create_app(bus)) as c:
        assert c.get("/office").status_code == 404
        assert c.get("/plugins/office/office.js").status_code == 404
        assert c.get("/api/health").json()["plugins"]["office"] == "disabled"
        items = {p["name"]: p for p in c.get("/api/plugins").json()["items"]}
        assert items["schedules"]["state"] == "missing"


def test_unknown_plugin_name_in_disable_is_a_warning(tmp_path):
    bus = make(tmp_path, plugins={"disable": ["nope"]})
    assert any("no plugin named 'nope'" in w for w in bus.config_warnings)


@pytest.mark.parametrize("strict", [False, True])
def test_config_of_a_missing_plugin_is_a_warning_not_an_error(tmp_path, monkeypatch, strict):
    monkeypatch.delenv("XQO_STRICT", raising=False)
    bus = make(tmp_path, schedules=[{"id": "s1", "every": "1h", "prompt": "x"}],
               channels={"tg": {"type": "telegram"}}, plugins={"strict": strict})
    assert bus.agents and bus.office  # the HQ runs
    assert any("plugin schedules is not included in xqo-lite; 'schedules'" in w for w in bus.config_warnings)
    assert any("plugin channels is not included" in w for w in bus.config_warnings)
    with TestClient(create_app(bus)) as c:
        assert c.get("/api/health").json()["plugins"]["schedules"].startswith("missing")
        assert c.get("/api/schedules").status_code == 404


def test_disabled_missing_plugin_with_config_says_nothing(tmp_path):
    bus = make(tmp_path, feeds={"sys": {"type": "system"}}, plugins={"disable": ["feeds"]})
    assert not any("feeds" in w for w in bus.config_warnings)


@pytest.mark.parametrize("strict", [False, True])
def test_a_plugin_that_cannot_be_imported_is_a_warning(tmp_path, monkeypatch, strict):
    real = plugin_mod.available
    monkeypatch.setattr(plugin_mod, "available", lambda: {**real(), "gone": "xqo_no_such_pkg.mod:Gone"})
    monkeypatch.delenv("XQO_STRICT", raising=False)
    bus = make(tmp_path, plugins={"strict": strict})
    st = bus.plugins.states["gone"]
    assert st.state == "missing" and "xqo_no_such_pkg" in st.error
    assert any("plugin gone is missing" in w for w in bus.config_warnings)


class Needy(Plugin):
    name = "needy"
    requires = ("schedules",)
    config_keys = ("needy",)

    def configure(self, ctx, cfg):
        return {"ok": True}


@pytest.mark.parametrize("strict", [False, True])
def test_a_requirement_that_is_missing_is_a_warning(tmp_path, monkeypatch, strict):
    real = plugin_mod.available
    monkeypatch.setattr(plugin_mod, "available", lambda: {**real(), "needy": f"{__name__}:Needy"})
    monkeypatch.delenv("XQO_STRICT", raising=False)
    bus = make(tmp_path, needy={}, plugins={"strict": strict})
    assert bus.plugins.states["needy"].state == "missing"
    assert any("plugin needy requires the schedules plugin, which is missing" in w for w in bus.config_warnings)


def test_office_views_2d_only(tmp_path):
    bus = make(tmp_path, office={"views": ["2d"]})
    with TestClient(create_app(bus)) as c:
        assert c.get("/api/office").json()["views"] == ["2d"]
        assert c.get("/plugins/office/office2d.js").status_code == 200
    with pytest.raises(ValueError, match="office.views"):
        make(tmp_path, office={"views": ["4d"]}, plugins={"strict": True})


class Boom(Plugin):
    name = "boom"
    tool_names = frozenset({"boom_tool"})

    def configure(self, ctx, cfg):
        if cfg.get("boom") == "configure":
            raise ValueError("bad boom config")
        return {"ok": True}

    async def start(self):
        if self.ctx.cfg.get("boom") == "start":
            raise RuntimeError("cannot start")

    def tool_defs(self, agent):
        return [{"name": "boom_tool", "description": "x", "input_schema": {"type": "object", "properties": {}}}]

    async def call_tool(self, task, agent, name, inp):
        return "boom!"

    def allows(self, task, agent, tool):
        if self.ctx.cfg.get("boom") == "gate":
            raise RuntimeError("gate broken")
        return tool != "forbidden_tool"

    def prompt_note(self, agent):
        return "BOOM NOTE"


@pytest.fixture
def boom(monkeypatch):
    real = plugin_mod.available
    monkeypatch.setattr(plugin_mod, "available", lambda: {**real(), "boom": f"{__name__}:Boom"})
    monkeypatch.delenv("XQO_STRICT", raising=False)


def test_third_party_plugin_hooks_reach_the_agent(tmp_path, boom):
    bus = make(tmp_path)
    assert bus.plugins.states["boom"].state == "active"
    a = bus.agents["planner"]
    assert "boom_tool" in [t["name"] for t in a.tools()[0]]
    assert "BOOM NOTE" in a._system()
    task = Task(id="t1", root_id="t1", prompt="p", requester="user", assignee="planner")
    from types import SimpleNamespace as NS
    r = asyncio.run(a._tool(task, NS(id="c1", name="boom_tool", input={})))
    assert r["content"] == "boom!" and not r.get("is_error")
    r = asyncio.run(a._tool(task, NS(id="c2", name="forbidden_tool", input={})))
    assert r["is_error"] and "boom plugin" in r["content"]


def test_a_gate_that_raises_fails_closed(tmp_path, boom):
    bus = make(tmp_path, boom="gate")
    a = bus.agents["planner"]
    task = Task(id="t1", root_id="t1", prompt="p", requester="user", assignee="planner")
    assert not bus.plugins.allows(task, a, "list_agents")
    from types import SimpleNamespace as NS
    r = asyncio.run(a._tool(task, NS(id="c1", name="list_agents", input={})))
    assert r["is_error"] and "blocked" in r["content"]


def test_configure_and_start_failures_are_isolated(tmp_path, boom):
    bus = make(tmp_path, boom="configure")
    assert bus.plugins.states["boom"].state == "failed" and "bad boom config" in bus.plugins.states["boom"].error
    bus = make(tmp_path, boom="start")
    with TestClient(create_app(bus)) as c:  # the lifespan starts plugins
        h = c.get("/api/health").json()
        assert h["plugins"]["boom"].startswith("failed") and "cannot start" in h["plugins"]["boom"]
        assert h["plugins"]["office"] == "active"


def test_strict_mode_stops_startup_on_a_real_error(tmp_path, boom, monkeypatch):
    with pytest.raises(ValueError, match="plugin boom: .*bad boom config"):
        make(tmp_path, boom="configure", plugins={"strict": True})
    monkeypatch.setenv("XQO_STRICT", "1")
    with pytest.raises(ValueError, match="bad boom config"):
        make(tmp_path, boom="configure")


def test_manager_rejects_bad_plugins_block(tmp_path):
    with pytest.raises(ValueError, match="unknown setting"):
        make(tmp_path, plugins={"enable": ["x"]})


def test_cli_has_no_commands_of_left_out_plugins():
    out = subprocess.run([sys.executable, "-m", "xqo", "--help"], capture_output=True, text=True).stdout
    assert "serve" in out
    for cmd in ("schedule", "channels", "peers", "agency"):
        assert cmd not in out, cmd
    assert isinstance(PluginManager, type)


# ---- in the browser (skipped without playwright / chromium) ------------------------------------------------
def _launch(p):
    import glob
    for exe in (None, *sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome"))):
        try:
            return p.chromium.launch(executable_path=exe)
        except Exception:
            continue
    pytest.skip("no chromium available for playwright")


def _page(tmp_path, **cfg):
    pw = pytest.importorskip("playwright.sync_api")
    from test_office import _Server
    bus = make(tmp_path, **cfg)
    return pw, _Server(create_app(bus))


def test_browser_2d_only_office_and_hidden_plugin_ui(tmp_path):
    pw, srv = _page(tmp_path, office={"views": ["2d"]})
    errors = []
    with srv, pw.sync_playwright() as p:
        b = _launch(p)
        pg = b.new_page()
        pg.on("pageerror", lambda e: errors.append(str(e)))
        requested = []
        pg.on("request", lambda r: requested.append(r.url))
        pg.goto(f"{srv.url}/office")
        pg.wait_for_function("window.office && window.office.r && window.office.cit.size > 0", timeout=20000)
        assert pg.evaluate("window.office.r.kind") == "2d"
        assert pg.inner_html("#views") == ""  # one view: no 3D / 2D switch
        assert not any("three" in u for u in requested)  # three.js is never loaded
        requested.clear()
        pg.goto(srv.url + "/")
        pg.wait_for_selector("#officelink")  # the office plugin's nav link
        pg.wait_for_function("document.getElementById('dot').classList.contains('on')")
        assert pg.query_selector("#tsched") is None  # schedules is not included: no button
        b.close()
    assert not errors, errors


def test_browser_dashboard_without_office(tmp_path):
    pw, srv = _page(tmp_path, plugins={"disable": ["office"]})
    errors = []
    with srv, pw.sync_playwright() as p:
        b = _launch(p)
        pg = b.new_page()
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(srv.url + "/")
        pg.wait_for_function("document.getElementById('dot').classList.contains('on')")
        assert pg.query_selector("#officelink") is None
        b.close()
    assert not errors, errors
