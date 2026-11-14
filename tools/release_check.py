"""Release checks: every package under packages/ has the same version, and it matches the tag.

    python tools/release_check.py 3.0.0     # exit 1 if any package's version differs
    python tools/release_check.py --list    # the package folders, base package first
"""
from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def packages() -> list[Path]:
    found = sorted(p.parent for p in (ROOT / "packages").glob("*/pyproject.toml"))
    return sorted(found, key=lambda p: p.name != "xqo")


def version(folder: Path, project: dict) -> str:
    """The static version, or the __version__ in the module that a dynamic version points at."""
    if "version" in project:
        return project["version"]
    attr = tomllib.loads((folder / "pyproject.toml").read_text())["tool"]["setuptools"]["dynamic"]["version"]["attr"]
    mod = attr.rpartition(".")[0].replace(".", "/")
    init = next(f for f in (folder / mod / "__init__.py", folder / "src" / mod / "__init__.py") if f.is_file())
    return re.search(r"""^__version__\s*=\s*["']([^"']+)""", init.read_text(), re.M).group(1)


def main(argv: list[str]) -> int:
    if argv == ["--list"]:
        print(" ".join(p.name for p in packages()))
        return 0
    if len(argv) != 1:
        print(__doc__)
        return 2
    want, bad = argv[0], []
    for p in packages():
        project = tomllib.loads((p / "pyproject.toml").read_text())["project"]
        if (v := version(p, project)) != want:
            bad.append(f"{project['name']} is {v}")
        pins = [d for e in project.get("optional-dependencies", {}).values() for d in e if d.startswith("xqo-")]
        bad += [f"{project['name']} extra pins {d}, not =={want}" for d in pins if not d.endswith(f"=={want}")]
    for line in bad:
        print(f"version mismatch with tag {want}: {line}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
