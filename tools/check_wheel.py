"""Build a package's wheel and check that its browser files and package data made it in.

    python tools/check_wheel.py packages/xqo

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
        names = set(zipfile.ZipFile(wheels[0]).namelist())
    missing = [f for f in REQUIRED.get(name, []) if f not in names]
    statics = [n for n in names if "/static/" in n]
    print(f"{wheels[0].name}: {len(names)} files, {len(statics)} static")
    if missing:
        print("missing from the wheel: " + ", ".join(missing))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "packages/xqo"))
