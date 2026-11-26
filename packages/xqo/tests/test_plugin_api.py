"""The plugin contract: the public API (xqo.api), a plugin installed from a wheel, and what plugins add to backups."""
import json
import subprocess
import sys
import zipfile

import xqo
from xqo import api, backup
from xqo import plugin as plugin_mod
from xqo.config import build


def test_api_exports_are_importable_and_versioned():
    assert api.__version__ == xqo.__version__ == "3.2.0"
    assert api.API_VERSION == 1 and api.ENTRY_POINT_GROUP == "xqo.plugins"
    missing = [n for n in api.__all__ if not hasattr(api, n)]
    assert not missing, missing
    assert api.Plugin.api == api.API_VERSION


# ---- a plugin package, built as a wheel and installed into its own folder (no network, no pip) -----------------
PLUGIN_SRC = '''
from pathlib import Path
from xqo.api import Plugin, API_VERSION

class Dummy(Plugin):
    name = "dummy"
    description = "contract test plugin"
    config_keys = ("dummy",)
    state_dirs = ("dummy-state",)
    skip_files = ("*.dummylock",)
    static = ("/plugins/dummy", Path(__file__).parent / "static")
    ui = ("ui.js",)

    def configure(self, ctx, cfg):
        return {"greeting": (cfg.get("dummy") or {}).get("greeting", "hi")}

    def status(self):
        return {"greeting": self.obj["greeting"]}

class TooNew(Plugin):
    name = "toonew"
    api = API_VERSION + 1
'''


def _wheel(tmp_path):
    whl = tmp_path / "xqo_dummy-0.1.0-py3-none-any.whl"
    di = "xqo_dummy-0.1.0.dist-info"
    files = {
        "xqo_dummy/__init__.py": PLUGIN_SRC,
        "xqo_dummy/static/ui.js": "window.dummyLoaded = true;\n",
        f"{di}/METADATA": "Metadata-Version: 2.1\nName: xqo-dummy\nVersion: 0.1.0\nRequires-Dist: xqo>=3.0,<4\n",
        f"{di}/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{di}/entry_points.txt": "[xqo.plugins]\ndummy = xqo_dummy:Dummy\ntoonew = xqo_dummy:TooNew\n",
    }
    files[f"{di}/RECORD"] = "".join(f"{n},,\n" for n in files) + f"{di}/RECORD,,\n"
    with zipfile.ZipFile(whl, "w") as z:
        for n, text in files.items():
            z.writestr(n, text)
    return whl


def _install(whl, target):
    """A pure-python wheel installs by unpacking it onto the path (what pip does for purelib wheels)."""
    with zipfile.ZipFile(whl) as z:
        z.extractall(target)


CHECK = '''
import json, sys
from pathlib import Path
from fastapi.testclient import TestClient
from xqo.config import build
from xqo.web import create_app
from xqo import backup
tmp = Path(sys.argv[1])
cfg = tmp / "agents.json"
cfg.write_text(json.dumps({"agents": [{"name": "planner", "skills": ["planning"]}],
                           "dummy": {"greeting": "hello"}}))
bus = build(cfg, tmp / "artifacts", str(tmp / "logs" / "events.jsonl"), mock=True, departments="none")
st = bus.plugins.states
out = {"dummy": st["dummy"].state, "source": st["dummy"].source, "toonew": st["toonew"].state,
       "toonew_error": st["toonew"].error, "warnings": bus.config_warnings,
       "state_dirs": sorted(backup.state_dirs()), "skip": list(backup.skip_files())}
with TestClient(create_app(bus)) as c:
    rows = {p["name"]: p for p in c.get("/api/plugins").json()["items"]}
    out["ui"] = rows["dummy"].get("ui")
    out["status"] = rows["dummy"].get("status")
    out["js"] = c.get("/plugins/dummy/ui.js").status_code
print(json.dumps(out))
'''


def test_a_plugin_installed_from_a_wheel_is_found_and_loaded(tmp_path):
    target = tmp_path / "site"
    _install(_wheel(tmp_path), target)
    work = tmp_path / "work"
    work.mkdir()
    env = {"PYTHONPATH": str(target), "PATH": "/usr/bin:/bin", "XQO_STRICT": "0"}
    r = subprocess.run([sys.executable, "-c", CHECK, str(work)], capture_output=True, text=True, env=env,
                       cwd=str(work), timeout=120)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["dummy"] == "active" and out["source"] == "xqo_dummy:Dummy"
    assert out["status"] == {"greeting": "hello"}
    assert out["ui"] == ["/plugins/dummy/ui.js"] and out["js"] == 200  # static files shipped in the wheel
    assert out["toonew"] == "failed" and "needs plugin API 2" in out["toonew_error"]  # API mismatch: a warning
    assert any("plugin toonew" in w for w in out["warnings"])
    assert "dummy-state" in out["state_dirs"] and "office-assets" in out["state_dirs"]
    assert "*.dummylock" in out["skip"]


class Keeper(plugin_mod.Plugin):
    name = "keeper"
    state_dirs = ("keeper-state",)
    skip_files = ("*.keeperlock",)
    config_files = ("keeper.json",)


def test_plugins_decide_what_backups_keep(tmp_path, monkeypatch):
    real = plugin_mod.available
    monkeypatch.setattr(plugin_mod, "available", lambda: {**real(), "keeper": f"{__name__}:Keeper"})
    saved = [set(r) for r in (plugin_mod.STATE_DIRS, plugin_mod.SKIP_FILES, plugin_mod.CONFIG_FILES)]
    try:
        cfg = tmp_path / "agents.json"
        cfg.write_text(json.dumps({"agents": [{"name": "planner", "skills": ["planning"]}]}))
        (tmp_path / "keeper.json").write_text("{}")
        bus = build(cfg, tmp_path / "artifacts", str(tmp_path / "logs" / "events.jsonl"), mock=True,
                    departments="none")
        root = tmp_path / "artifacts"
        (root / "keeper-state").mkdir(parents=True)
        (root / "keeper-state" / "settings.json").write_text("{}")
        (root / "keeper-state" / "run.keeperlock").write_text("1")
        (root / "other-folder").mkdir()
        (root / "other-folder" / "x.json").write_text("{}")
        data = backup.data_files(bus, "state", tmp_path)
        assert "data/artifacts/keeper-state/settings.json" in data
        assert "data/artifacts/keeper-state/run.keeperlock" not in data
        assert not any("other-folder" in k for k in data)  # no plugin claims it: not runtime settings
        assert "config/keeper.json" in backup.config_files(bus)
    finally:
        for reg, old in zip((plugin_mod.STATE_DIRS, plugin_mod.SKIP_FILES, plugin_mod.CONFIG_FILES), saved):
            reg.clear()
            reg.update(old)


def test_core_names_no_optional_feature():
    """The core finds plugin services through the manager, not through fixed bus slots."""
    import inspect
    from xqo import bus as bus_mod, web
    src = inspect.getsource(bus_mod) + inspect.getsource(web)
    for name in ("bus.scheduler", "self.scheduler", "bus.peers", "self.peers", "bus.channels", "bus.feeds"):
        assert name not in src, name
