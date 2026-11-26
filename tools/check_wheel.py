"""Build a package's wheel and check that its browser files and package data made it in.

    python tools/check_wheel.py packages/xqo
    python tools/check_wheel.py packages/xqo-sqlite

A wheel without its static files installs fine but the dashboard UI silently disappears, so CI checks it.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

# files every wheel of a package must contain (paths inside the wheel)
REQUIRED = {
    "xqo": ["xqo/static/index.html", "xqo/static/js/core.js", "xqo/default_agents.json",
            "xqo/plugins/office/static/office.js", "xqo/plugins/office/static/office3d.js",
            "xqo/plugins/office/static/office2d.js"],
    "xqo-schedules": ["xqo_schedules/static/ui.js"],
    "xqo-feeds": ["xqo_feeds/static/ui.js"],
    "xqo-peering": ["xqo_peering/static/ui.js"],
    "xqo-flows": ["xqo_flows/static/ui.js", "xqo_flows/static/editor.html", "xqo_flows/static/editor.js",
                  "xqo_flows/static/editor.css", "xqo_flows/examples/hello.json"],
}


def main(folder: str) -> int:
    pkg = Path(folder)
    name = pkg.name
    with tempfile.TemporaryDirectory() as out:
        subprocess.run(["uv", "build", "--wheel", "--out-dir", out, str(pkg)], check=True)
        wheels = list(Path(out).glob("*.whl"))
        if len(wheels) != 1:
            print(f"expected one wheel, got {wheels}")
            return 1
        with zipfile.ZipFile(wheels[0]) as z:
            names = set(z.namelist())
            eps = [n for n in names if n.endswith(".dist-info/entry_points.txt")]
            entry_points = z.read(eps[0]).decode() if eps else ""
    missing = [f for f in REQUIRED.get(name, []) if f not in names]
    if name != "xqo":  # a plugin package: its module, and the entry point that lets xqo find it
        mod = name.replace("-", "_")
        if f"{mod}/__init__.py" not in names:
            missing.append(f"{mod}/__init__.py")
        if "[xqo.plugins]" not in entry_points:
            missing.append("an [xqo.plugins] entry point")
    statics = [n for n in names if "/static/" in n]
    print(f"{wheels[0].name}: {len(names)} files, {len(statics)} static")
    if missing:
        print("missing from the wheel: " + ", ".join(missing))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "packages/xqo"))
