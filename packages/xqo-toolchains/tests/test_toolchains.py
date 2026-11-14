"""Node/Go/Rust/.NET toolchains on the local sandbox backend: components, versions, caches, dependencies, backends."""
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from xqo_toolchains import chains as tc
from xqo.sandbox import (BACKENDS, Backend, LocalBackend, RUN_COPY_SKIP, Sandbox, SandboxConfig, backend_class,
                                  format_result, is_python_project, register_backend)

posix = pytest.mark.skipif(os.name == "nt", reason="fake tools are sh scripts")
try:
    import xqo_docker  # noqa: F401  (registered in conftest)
except ImportError:
    xqo_docker = None
docker_plugin = pytest.mark.skipif(xqo_docker is None, reason="xqo-docker is not installed")


def write(p: Path, text: str = "") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def tool(bindir: Path, name: str, body: str) -> None:
    f = write(bindir / name, "#!/bin/sh\n" + body)
    f.chmod(0o755)


def box(tmp_path, **kw) -> Sandbox:
    return Sandbox(SandboxConfig(**{"backend": "local", "timeout": 60, "cache_dir": str(tmp_path / "cache"), **kw}))


def run(sb, ws, cmd, tmp_path):
    return asyncio.run(sb.run(ws, cmd, env_dir=tmp_path / "task" / ".venv"))


@pytest.fixture
def fake_npm(tmp_path, monkeypatch):
    """An `npm` that installs package `hello` (from package.json) into ./node_modules and counts its calls."""
    bindir = tmp_path / "fakebin"
    tool(bindir, "npm", f'echo "$@" >> "{tmp_path}/npm.calls"\n'
                        'mkdir -p node_modules/hello && echo "module.exports = \\"hi from hello\\"" '
                        '> node_modules/hello/index.js\necho "added 1 package"\n')
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return tmp_path / "npm.calls"


# ---- detection -------------------------------------------------------------------------------------------------------
def test_components_of_a_mixed_workspace(tmp_path):
    ws = tmp_path / "ws"
    write(ws / "go.mod", "module x\n\ngo 1.21\n")
    write(ws / "web/package.json", "{}")
    write(ws / "web/node_modules/a/package.json", "{}")       # never a component
    write(ws / "api/Api.csproj", "<Project/>")
    write(ws / "engine/Cargo.toml", "[package]\nname='e'\n")
    write(ws / "requirements.txt", "")
    write(ws / "a/b/c/d/package.json", "{}")                   # deeper than MAX_DEPTH
    comps = {(c.path, c.kind) for c in tc.detect(ws)}
    assert comps == {(".", "go"), (".", "python"), ("web", "node"), ("api", "dotnet"), ("engine", "rust")}


def test_workspace_roots_own_their_members(tmp_path):
    ws = tmp_path / "ws"
    write(ws / "package.json", json.dumps({"workspaces": ["packages/*"]}))
    write(ws / "packages/a/package.json", "{}")
    write(ws / "Cargo.toml", "[workspace]\nmembers = ['crates/x']\n")
    write(ws / "crates/x/Cargo.toml", "[package]\nname='x'\n")
    write(ws / "tools/ui/package.json", "{}")  # below the npm workspace root: part of it
    comps = tc.detect(ws)
    assert [(c.path, c.kind, c.mode) for c in comps] == [(".", "node", "inplace"), (".", "rust", "")]


def test_overrides_replace_detection(tmp_path):
    ws = tmp_path / "ws"
    write(ws / "package.json", "{}")
    comps = tc.detect(ws, [{"path": "frontend/", "toolchain": "node"}, {"path": ".", "toolchain": "cobol"}])
    assert [(c.path, c.kind) for c in comps] == [("frontend", "node")]


def test_node_modes(tmp_path):
    d = tmp_path / "p"
    write(d / "package.json", json.dumps({"dependencies": {"left-pad": "1"}}))
    assert tc._node_mode(d, "auto") == "link"
    write(d / "package.json", json.dumps({"dependencies": {"next": "15"}}))
    assert tc._node_mode(d, "auto") == "clone" and tc._node_mode(d, "link") == "link"
    write(d / "package.json", json.dumps({"dependencies": {"x": "file:../x"}}))
    assert tc._node_mode(d, "link") == "inplace"


def test_python_detection_ignores_node_modules_and_other_stacks(tmp_path):
    ws = tmp_path / "ws"
    write(ws / "package.json", "{}")
    write(ws / "node_modules/node-gyp/gyp/gyp_main.py", "")
    write(ws / "scripts/gen.py", "")
    assert not is_python_project(ws)          # Node project with a helper script: no venv
    write(ws / "pyproject.toml", "[project]\nname='x'\n")
    assert is_python_project(ws)              # Python markers win
    plain = tmp_path / "plain"
    write(plain / "fib.py", "")
    assert is_python_project(plain)           # loose scripts, nothing else: still a Python project
    write(tmp_path / "nm/node_modules/x/y.py", "")
    assert not is_python_project(tmp_path / "nm")


def test_wanted_versions(tmp_path):
    ws = tmp_path / "ws"
    write(ws / "web/package.json", json.dumps({"engines": {"node": ">=18"}}))
    write(ws / "go.mod", "module x\n\ngo 1.22\ntoolchain go1.22.3\n")
    write(ws / "global.json", json.dumps({"sdk": {"version": "8.0.100"}}))
    write(ws / "api/A.csproj", "<Project/>")
    comps = tc.detect(ws)
    assert tc.wanted_versions(ws, comps) == {"go": "1.22.3", "dotnet": "8.0.100"}  # a range is not a version
    write(ws / "web/package.json", json.dumps({"engines": {"node": "20.x"}}))
    assert tc.wanted_versions(ws, comps)["node"] == "20"
    write(ws / ".nvmrc", "v18.17.0\n")
    assert tc.wanted_versions(ws, comps)["node"] == "18.17.0"
    assert tc.version_matches("18.17.0", "18.20.1", "node") and not tc.version_matches("18", "22.1.0", "node")
    assert tc.version_matches("1.22", "1.22.5", "go") and not tc.version_matches("1.21", "1.22.5", "go")


def test_copy_skips_build_outputs_next_to_their_project_only(tmp_path):
    ws = tmp_path / "ws"
    write(ws / "web/package.json", "{}")
    write(ws / "web/node_modules/a/index.js", "")
    write(ws / "web/.next/x", "")
    write(ws / "rs/Cargo.toml", "[package]\nname='r'\n")
    write(ws / "rs/target/debug/r", "")
    write(ws / "docs/target/keep.md", "")      # a folder called target that is not cargo's
    write(ws / "api/A.csproj", "<Project/>")
    write(ws / "api/bin/Debug/a.dll", "")
    comps = tc.detect(ws)
    dst = tmp_path / "copy"
    shutil.copytree(ws, dst, ignore=tc.copy_ignore(ws, comps, RUN_COPY_SKIP))
    assert not (dst / "web/node_modules").exists() and not (dst / "web/.next").exists()
    assert not (dst / "rs/target").exists() and not (dst / "api/bin").exists()
    assert (dst / "docs/target/keep.md").exists() and (dst / "web/package.json").exists()
    dst2 = tmp_path / "copy2"  # toolchains off: node_modules still reaches the run, as before
    shutil.copytree(ws, dst2, ignore=tc.copy_ignore(ws, [], RUN_COPY_SKIP))
    assert (dst2 / "web/node_modules/a/index.js").exists()


# ---- node ------------------------------------------------------------------------------------------------------------
@posix
@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_node_deps_installed_once_linked_and_reinstalled_on_change(tmp_path, fake_npm):
    ws = tmp_path / "ws"
    write(ws / "package.json", json.dumps({"name": "app", "dependencies": {"hello": "1.0.0"}}))
    write(ws / "package-lock.json", "{}")
    sb = box(tmp_path)
    r = run(sb, ws, "node -e \"console.log(require('hello'))\"", tmp_path)
    assert r["exit_code"] == 0, format_result(r)
    assert r["stdout"].strip() == "hi from hello"
    info = r["toolchains"][0]
    assert (info["kind"], info["path"], info["manager"], info["node_modules"]) == ("node", ".", "npm", "linked")
    assert "installed with npm ci" in info["setup"] and "[node .: installed with npm ci" in format_result(r)
    assert fake_npm.read_text().splitlines() == ["ci --no-audit --no-fund"]
    assert not (ws / "node_modules").exists()  # installed under the task's .deps, never in the workspace
    assert (tmp_path / "task/.deps/node@root/pkg/node_modules/hello/index.js").is_file()
    assert "venv" not in r  # a Node project gets no Python venv
    r = run(sb, ws, "node -e \"console.log(require('hello'))\"", tmp_path)
    assert r["exit_code"] == 0 and r["toolchains"][0].get("reused")
    assert len(fake_npm.read_text().splitlines()) == 1  # unchanged manifests: no second install
    write(ws / "package.json", json.dumps({"name": "app", "dependencies": {"hello": "1.0.1"}}))
    r = run(sb, ws, "true", tmp_path)
    assert not r["toolchains"][0].get("reused") and len(fake_npm.read_text().splitlines()) == 2


@posix
def test_node_env_caches_and_install_note(tmp_path, fake_npm):
    ws = tmp_path / "ws"
    write(ws / "package.json", json.dumps({"dependencies": {"next": "15"}}))
    sb = box(tmp_path, install_scripts=False)
    r = run(sb, ws, 'echo "$npm_config_cache|$NEXT_TELEMETRY_DISABLED"; test -L node_modules || echo realdir; '
                    'npm install left-pad', tmp_path)
    assert r["exit_code"] == 0, format_result(r)
    cache, tele = r["stdout"].splitlines()[0].split("|")
    assert Path(cache) == (tmp_path / "cache" / "npm").resolve() and tele == "1"
    assert "realdir" in r["stdout"]  # Next gets a real (hard-linked) node_modules: Turbopack rejects outside links
    assert fake_npm.read_text().splitlines()[0] == "install --no-audit --no-fund --ignore-scripts"
    assert r["notes"] and "thrown away" in format_result(r)


@posix
def test_missing_package_manager_is_reported(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    write(ws / "package.json", json.dumps({"packageManager": "pnpm@9.0.0"}))
    write(ws / "pnpm-lock.yaml", "")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # no pnpm, no corepack
    r = run(box(tmp_path), ws, "echo ran", tmp_path)
    assert r["exit_code"] == 0 and r["stdout"].strip() == "ran"  # the command still runs
    assert "pnpm is not installed" in r["toolchains"][0]["error"] and "setup ERROR" in format_result(r)


def test_toolchains_off_keeps_the_old_behaviour(tmp_path):
    ws = tmp_path / "ws"
    write(ws / "package.json", "{}")
    write(ws / "node_modules/a/index.js", "x")
    r = run(box(tmp_path, toolchains=False), ws, "ls node_modules", tmp_path)
    assert r["exit_code"] == 0 and "a" in r["stdout"] and "toolchains" not in r


def test_without_the_toolchains_plugin_runs_still_work(tmp_path, monkeypatch, fake_npm):
    import xqo.sandbox as sbmod
    monkeypatch.setattr(sbmod, "toolchains", None)  # = "plugins": {"disable": ["toolchains"]}
    ws = tmp_path / "ws"
    write(ws / "package.json", json.dumps({"dependencies": {"hello": "1"}}))
    write(ws / "app.py", "print(1)")
    r = run(box(tmp_path), ws, "ls", tmp_path)
    assert r["exit_code"] == 0 and "toolchains" not in r and not fake_npm.exists()
    assert sbmod.is_python_project(ws) and "package.json" in format_result(r)


# ---- versions --------------------------------------------------------------------------------------------------------
@posix
def test_mise_installs_wanted_versions_and_puts_them_first(tmp_path, monkeypatch):
    bindir, node18 = tmp_path / "fakebin", tmp_path / "mise/installs/node/18/bin"
    tool(node18, "node", 'echo "v18.20.4"\n')
    tool(bindir, "mise", f'echo "$@" >> "{tmp_path}/mise.calls"\n'
                         f'if [ "$1" = env ]; then echo "warning on stderr" >&2; '
                         f'echo "{{\\"PATH\\": \\"{node18}:$PATH\\", \\"NODE_HOME\\": \\"{node18.parent}\\"}}"; fi\n')
    tool(bindir, "npm", 'mkdir -p node_modules\n')
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    ws = tmp_path / "ws"
    write(ws / "package.json", "{}")
    write(ws / ".nvmrc", "18\n")
    sb = box(tmp_path)
    r = run(sb, ws, 'node --version; echo "$NODE_HOME"', tmp_path)
    assert r["exit_code"] == 0, format_result(r)
    assert r["stdout"].splitlines() == ["v18.20.4", str(node18.parent)]
    v = r["toolchains"][0]
    assert v["manager"] == "mise" and v["tools"] == {"node": "18"} and "[versions via mise: node 18]" in format_result(r)
    assert (tmp_path / "mise.calls").read_text().splitlines() == ["install node@18", "env --json node@18"]
    r = run(sb, ws, "node --version", tmp_path)
    assert r["stdout"].strip() == "v18.20.4" and r["toolchains"][0].get("reused")
    assert len((tmp_path / "mise.calls").read_text().splitlines()) == 2  # resolved once per task


@posix
def test_host_version_mismatch_is_a_warning(tmp_path, monkeypatch):
    bindir = tmp_path / "fakebin"
    tool(bindir, "node", 'echo "v22.3.0"\n')
    tool(bindir, "npm", 'mkdir -p node_modules\n')
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}/usr/bin:/bin")
    ws = tmp_path / "ws"
    write(ws / "package.json", "{}")
    write(ws / ".node-version", "12.22.0")
    r = run(box(tmp_path, versions="host"), ws, "true", tmp_path)
    assert "the project wants node 12.22.0, the host has 22.3.0" in format_result(r)


# ---- go / rust / dotnet ----------------------------------------------------------------------------------------------
@pytest.mark.skipif(not shutil.which("go"), reason="go not installed")
def test_real_go_uses_task_build_cache_and_shared_module_cache(tmp_path):
    ws = tmp_path / "ws"
    write(ws / "go.mod", "module example.com/hi\n\ngo 1.20\n")
    write(ws / "main.go", 'package main\n\nimport "fmt"\n\nfunc main() { fmt.Println("hi from go") }\n')
    r = run(box(tmp_path, timeout=300), ws, "go run .", tmp_path)
    assert r["exit_code"] == 0, format_result(r)
    assert r["stdout"].strip() == "hi from go"
    assert r["toolchains"][0]["setup"] == "no dependencies"
    assert any((tmp_path / "task/.deps/go/build").iterdir())


@pytest.mark.skipif(not shutil.which("cargo"), reason="cargo not installed")
def test_real_cargo_builds_into_the_task_target_dir(tmp_path):
    ws = tmp_path / "ws"
    write(ws / "Cargo.toml", '[package]\nname = "hi"\nversion = "0.1.0"\nedition = "2021"\n')
    write(ws / "src/main.rs", 'fn main() { println!("hi from rust"); }\n')
    r = run(box(tmp_path, timeout=300), ws, "cargo run -q", tmp_path)
    assert r["exit_code"] == 0, format_result(r)  # HOME is the run copy: rustup still finds its toolchains
    assert r["stdout"].strip() == "hi from rust"
    assert (tmp_path / "task/.deps/rust-target").is_dir() and not (ws / "target").exists()


@posix
def test_dotnet_restore_targets_the_solution_and_disables_build_servers(tmp_path, monkeypatch):
    bindir = tmp_path / "fakebin"
    tool(bindir, "dotnet", f'echo "$@" >> "{tmp_path}/dotnet.calls"\n')
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    ws = tmp_path / "ws"
    write(ws / "App.sln", "")
    write(ws / "src/App/App.csproj", "<Project/>")
    r = run(box(tmp_path), ws, 'echo "$MSBUILDDISABLENODEREUSE $UseSharedCompilation $NUGET_PACKAGES"', tmp_path)
    assert r["exit_code"] == 0, format_result(r)
    reuse, shared, nuget = r["stdout"].split()
    assert (reuse, shared, Path(nuget)) == ("1", "false", (tmp_path / "cache/nuget").resolve())
    assert (tmp_path / "dotnet.calls").read_text().splitlines() == ["restore App.sln"]
    assert [(c["kind"], c["path"]) for c in r["toolchains"]] == [("dotnet", ".")]  # the .sln owns src/App


@posix
@pytest.mark.skipif(not shutil.which("node") or not shutil.which("go"), reason="node and go needed")
def test_go_with_node_frontend_in_one_run(tmp_path, fake_npm):
    ws = tmp_path / "ws"
    write(ws / "go.mod", "module example.com/app\n\ngo 1.20\n")
    write(ws / "main.go", 'package main\n\nimport "fmt"\n\nfunc main() { fmt.Println("api ok") }\n')
    write(ws / "web/package.json", json.dumps({"dependencies": {"hello": "1"}}))
    r = run(box(tmp_path, timeout=300), ws,
            "cd web && node -e \"console.log(require('hello'))\" && cd .. && go run .", tmp_path)
    assert r["exit_code"] == 0, format_result(r)
    assert r["stdout"].split("\n")[:2] == ["hi from hello", "api ok"]
    assert sorted((c["kind"], c["path"]) for c in r["toolchains"]) == [("go", "."), ("node", "web")]


# ---- backends --------------------------------------------------------------------------------------------------------
def test_unknown_backend_stops_startup():
    with pytest.raises(ValueError, match="unknown sandbox backend 'remote'"):
        Sandbox(SandboxConfig(backend="remote"))
    with pytest.raises(ValueError, match="could not be loaded"):
        Sandbox(SandboxConfig(backend="no.such.module:Thing"))
    assert backend_class("xqo.sandbox:LocalBackend") is LocalBackend
    assert set(BACKENDS) >= ({"local", "docker"} if xqo_docker else {"local"})


def test_a_registered_backend_runs_commands(tmp_path):
    @register_backend
    class Echo(Backend):
        name = "echo-test"

        async def command(self, sb, work, command, env_dir, comps):
            return {"argv": [sys.executable, "-c", f"print('remote ran: ' + {command!r})"], "env": None,
                    "run_cmd": command, "result": {"where": "far away"}}
    try:
        r = asyncio.run(Sandbox(SandboxConfig(backend="echo-test")).run(tmp_path / "ws", "make test"))
        assert r["backend"] == "echo-test" and r["stdout"].strip() == "remote ran: make test"
        assert r["where"] == "far away" and r["exit_code"] == 0
    finally:
        BACKENDS.pop("echo-test", None)


# ---- docker ----------------------------------------------------------------------------------------------------------
def test_docker_images_follow_the_project_versions(tmp_path):
    ws = tmp_path / "ws"
    write(ws / "package.json", "{}")
    write(ws / ".nvmrc", "18.17.0")
    cfg = SandboxConfig()
    assert tc.docker_plan(ws, tc.detect(ws), cfg) == {"image": "node:18.17.0-bookworm", "dockerfile": None,
                                                       "tools": {"node": "node:18.17.0-bookworm"}}
    go = tmp_path / "go"
    write(go / "go.mod", "module x\n\ngo 1.22\ntoolchain go1.22.3\n")
    assert tc.docker_plan(go, tc.detect(go), cfg)["image"] == "golang:1.22.3-bookworm"
    rs = tmp_path / "rs"
    write(rs / "Cargo.toml", "[package]\nname='r'\n")
    write(rs / "rust-toolchain.toml", '[toolchain]\nchannel = "1.79"\n')
    assert tc.docker_plan(rs, tc.detect(rs), cfg)["image"] == "rust:1.79-bookworm"
    write(rs / "rust-toolchain.toml", '[toolchain]\nchannel = "stable"\n')
    assert tc.docker_plan(rs, tc.detect(rs), cfg)["image"] == "rust:1-bookworm"
    dn = tmp_path / "dn"
    write(dn / "A.csproj", "<Project/>")
    write(dn / "global.json", json.dumps({"sdk": {"version": "9.0.100"}}))
    assert tc.docker_plan(dn, tc.detect(dn), cfg)["image"] == "mcr.microsoft.com/dotnet/sdk:9.0"
    assert tc.docker_plan(ws, tc.detect(ws), SandboxConfig(images={"node": "my/node:1"}))["image"] == "my/node:1"


def test_mixed_projects_get_one_composite_image(tmp_path):
    ws = tmp_path / "ws"
    write(ws / "go.mod", "module x\n\ngo 1.22\n")
    write(ws / "web/package.json", "{}")
    write(ws / "requirements.txt", "")
    plan = tc.docker_plan(ws, tc.detect(ws), SandboxConfig())
    df = plan["dockerfile"]
    assert plan["image"].startswith("orch-toolchains:") and df.startswith("FROM buildpack-deps:bookworm\n")
    assert "COPY --from=golang:1.22-bookworm /usr/local/go /usr/local/go" in df
    assert "COPY --from=node:22-bookworm /usr/local/bin/node" in df and f"COPY --from={tc.UV_IMAGE} /uv" in df
    assert plan["tools"] == {"go": "golang:1.22-bookworm", "node": "node:22-bookworm", "python": "uv"}
    write(ws / "go.mod", "module x\n\ngo 1.23\n")  # another version: another image
    assert tc.docker_plan(ws, tc.detect(ws), SandboxConfig())["image"] != plan["image"]


FAKE_DOCKER_PY = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
a = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a") as f:
    f.write(json.dumps(a) + "\n")
if a[:2] == ["image", "inspect"]:
    sys.exit(0 if Path(os.environ["FAKE_DOCKER_LOG"] + ".built").exists() else 1)
if a[:1] == ["build"]:
    Path(os.environ["FAKE_DOCKER_LOG"] + ".built").touch(); sys.exit(0)
if a[:1] == ["run"]:
    mounts = dict(reversed(v.split(":")[:2]) for k, v in zip(a, a[1:]) if k == "-v")
    host = lambda p: next((Path(h) / os.path.relpath(p, c) for c, h in mounts.items() if p == c or p.startswith(c + "/")), None)
    wd = a[a.index("-w") + 1]
    if "npm" in a:  # a setup container installing: make node_modules where it runs
        (host(wd) / "node_modules" / "hello").mkdir(parents=True, exist_ok=True)
    work = Path(mounts["/work"])
    nm = work / "node_modules"
    if nm.is_symlink():
        print("node_modules ->", os.readlink(nm))
'''


@posix
@docker_plugin
def test_docker_setup_containers_install_and_runs_mount_deps_offline(tmp_path, monkeypatch):
    bindir, log = tmp_path / "fakebin", tmp_path / "docker.log"
    write(bindir / "docker", FAKE_DOCKER_PY).chmod(0o755)
    ca = write(tmp_path / "ca.pem", "-----BEGIN CERTIFICATE-----\n")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))
    monkeypatch.setenv("SSL_CERT_FILE", str(ca))
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:3128")  # loopback: useless inside a container
    ws = tmp_path / "ws"
    write(ws / "package.json", json.dumps({"dependencies": {"hello": "1"}}))
    write(ws / "package-lock.json", "{}")
    write(ws / "node_modules/stale/index.js", "")  # the workspace's own node_modules never reaches the run
    sb = Sandbox(SandboxConfig(backend="docker", cache_dir=str(tmp_path / "cache")))
    r = run(sb, ws, "npm test", tmp_path)
    calls = [json.loads(x) for x in log.read_text().splitlines()]
    setup, runc = calls
    deps, cache = tmp_path / "task/.deps-docker", (tmp_path / "cache/docker").resolve()
    assert setup[-4:] == ["npm", "ci", "--no-audit", "--no-fund"] and "node:22-bookworm" in setup
    assert setup[setup.index("-w") + 1] == "/deps/node@root/pkg" and f"{deps}:/deps" in setup and f"{cache}:/cache" in setup
    assert "npm_config_cache=/cache/npm" in setup and "--network" not in setup
    assert f"{ca.resolve()}:/etc/ssl/orch-ca.pem:ro" in setup and "NODE_EXTRA_CA_CERTS=/etc/ssl/orch-ca.pem" in setup
    assert not any("3128" in x for x in setup)
    assert runc[-4:] == ["node:22-bookworm", "sh", "-c", "npm test"] and runc[runc.index("--network") + 1] == "none"
    assert "GOPROXY=off" in runc and "npm_config_cache=/cache/npm" in runc and f"{deps}:/deps" in runc
    assert not any("orch-ca" in x for x in runc)  # the run is offline: no CA, no proxies
    assert r["stdout"].strip() == "node_modules -> /deps/node@root/pkg/node_modules"
    assert (deps / "node@root/pkg/node_modules/hello").is_dir()
    assert "[image node:22-bookworm]" in format_result(r) and "[node .: installed with npm ci" in format_result(r)
    r = run(sb, ws, "npm test", tmp_path)
    assert r["toolchains"][1].get("reused") and len(log.read_text().splitlines()) == 3  # no second setup


@posix
@docker_plugin
def test_docker_composite_image_built_once_and_used_for_the_venv(tmp_path, monkeypatch):
    bindir, log = tmp_path / "fakebin", tmp_path / "docker.log"
    write(bindir / "docker", FAKE_DOCKER_PY).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))
    ws = tmp_path / "ws"
    write(ws / "go.mod", "module x\n\ngo 1.22\n")
    write(ws / "requirements.txt", "")
    sb = Sandbox(SandboxConfig(backend="docker", cache_dir=str(tmp_path / "cache")))
    r = run(sb, ws, "go test ./... && pytest", tmp_path)
    calls = [json.loads(x) for x in log.read_text().splitlines()]
    image = r["toolchains"][0]["image"]
    assert [c[0] for c in calls] == ["image", "build", "run", "run"]  # inspect, build, venv setup, run
    assert calls[1][:4] == ["build", "-q", "-t", image] and "built" in format_result(r)
    venv_setup, runc = calls[2], calls[3]
    assert image in venv_setup and runc[-4:] == [image, "sh", "-c", "go test ./... && pytest"]
    assert "VIRTUAL_ENV=/venv/env" in runc and any(x.startswith("PATH=/venv/env/bin:") and "/usr/local/go/bin" in x
                                                   for x in runc)
    assert r["toolchains"][1]["setup"] == "no dependencies"
    run(sb, ws, "true", tmp_path)
    assert sum(c[0] == "build" for c in (json.loads(x) for x in log.read_text().splitlines())) == 1


def _images(*names):
    import subprocess
    from xqo.sandbox import docker_available
    return xqo_docker is not None and docker_available() and all(subprocess.run(["docker", "image", "inspect", n], capture_output=True)
                                      .returncode == 0 for n in names)


@pytest.mark.skipif(not _images("golang:bookworm", "node:22-bookworm-slim", tc.COMPOSITE_BASE),
                    reason="docker or its images are not available")
def test_real_docker_go_and_node_in_a_composite_image(tmp_path):
    ws = tmp_path / "ws"
    write(ws / "go.mod", "module example.com/app\n\ngo 1.20\n")
    write(ws / "main.go", 'package main\n\nimport "fmt"\n\nfunc main() { fmt.Println("api ok") }\n')
    write(ws / "web/package.json", json.dumps({"name": "web", "version": "1.0.0"}))
    sb = Sandbox(SandboxConfig(backend="docker", timeout=600, setup_timeout=900, cache_dir=str(tmp_path / "cache"),
                               images={"go": "golang:bookworm", "node": "node:22-bookworm-slim"}))
    r = run(sb, ws, "cd web && node -e \"console.log(require('fs').realpathSync('node_modules'))\" && cd .. && go run .",
            tmp_path)
    assert r["exit_code"] == 0, format_result(r)
    assert r["stdout"].splitlines() == ["/deps/node@web/pkg/node_modules", "api ok"]


@pytest.mark.skipif(not _images("rust:1-bookworm"), reason="docker or the rust image is not available")
def test_real_docker_rust_target_dir_is_kept_per_task(tmp_path):
    ws = tmp_path / "ws"
    write(ws / "Cargo.toml", '[package]\nname = "hi"\nversion = "0.1.0"\nedition = "2021"\n')
    write(ws / "src/main.rs", 'fn main() { println!("hi from rust"); }\n')
    sb = Sandbox(SandboxConfig(backend="docker", timeout=600, cache_dir=str(tmp_path / "cache")))
    r = run(sb, ws, "cargo run -q", tmp_path)
    assert r["exit_code"] == 0, format_result(r)
    assert r["stdout"].strip() == "hi from rust" and any((tmp_path / "task/.deps-docker/rust-target").iterdir())


@pytest.mark.skipif(not _images("mcr.microsoft.com/dotnet/sdk:8.0"), reason="docker or the dotnet image is not available")
def test_real_docker_dotnet_restores_and_runs(tmp_path):
    ws = tmp_path / "ws"
    write(ws / "App.csproj", '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType>'
                             '<TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>')
    write(ws / "Program.cs", 'System.Console.WriteLine("hi from dotnet");\n')
    sb = Sandbox(SandboxConfig(backend="docker", timeout=600, memory="2g", cache_dir=str(tmp_path / "cache")))
    r = run(sb, ws, "dotnet run", tmp_path)
    assert r["exit_code"] == 0, format_result(r)
    assert r["stdout"].strip() == "hi from dotnet"
