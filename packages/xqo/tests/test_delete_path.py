"""delete_path: agents delete workspace files and whole folders (e.g. a leftover node_modules) with a tool,
since shell commands only change a throwaway copy of the workspace."""
import asyncio
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from xqo.artifacts import ArtifactStore
from xqo.config import build
from xqo.gitstore import GitConfig, GitRepo, git_available
from xqo.models import Task
from xqo.patching import PatchError
from xqo.sandbox import RUN_COPY_SKIP

ROOT = Path(__file__).resolve().parent.parent


def _node_modules(ws: Path) -> None:
    for rel in ("node_modules/a/index.js", "node_modules/a/node_modules/b/x.bin", "node_modules/.package-lock.json"):
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\x00\x01data")
    ro = ws / "node_modules/a/index.js"
    os.chmod(ro, stat.S_IREAD)  # read-only files (npm on Windows, git objects) must not stop the delete


def test_delete_path_removes_a_folder_and_records_it_once(tmp_path):
    s = ArtifactStore(tmp_path / "art")
    s.write_file("r", "src/app.js", "x\n", "t1", "coder")
    s.write_file("r", "build/out.js", "y\n", "t1", "coder")
    _node_modules(s.workspace("r"))
    rec = s.delete_path("r", "node_modules", "t2", "coder")
    assert (rec["action"], rec["file"], rec["folder"], rec["files"]) == ("delete", "node_modules", True, 3)
    assert not (s.workspace("r") / "node_modules").exists()
    rec = s.delete_path("r", "build/", "t2", "coder")
    assert rec["folder"] and rec["files"] == 1
    assert {f["file"] for f in s.list_files("r")} == {"src/app.js"}
    assert s.state_from(s.list("t1") + s.list("t2")) == {"src/app.js": "x\n"}


def test_delete_path_deletes_a_single_file(tmp_path):
    s = ArtifactStore(tmp_path / "art")
    s.write_file("r", "a.py", "a\n", "t1", "coder")
    rec = s.delete_path("r", "a.py", "t2", "coder")
    assert rec["action"] == "delete" and not rec.get("folder")
    assert s.list_files("r") == []


@pytest.mark.parametrize("path,msg", [("missing", "not in workspace"), (".git", ".git folder"),
                                      (".", "invalid file path"), ("", "invalid file path")])
def test_delete_path_refusals(tmp_path, path, msg):
    s = ArtifactStore(tmp_path / "art")
    s.write_file("r", "a.py", "a\n", "t1", "coder")
    with pytest.raises(PatchError, match=msg):
        s.delete_path("r", path, "t1", "coder")
    assert s.read_file("r", "a.py") == "a\n"


def test_delete_path_stays_inside_the_workspace(tmp_path):
    s = ArtifactStore(tmp_path / "art")
    s.write_file("r", "a.py", "a\n", "t1", "coder")
    outside = tmp_path / "art" / "keep.txt"
    outside.write_text("k")
    with pytest.raises(PatchError):
        s.delete_path("r", "../keep.txt", "t1", "coder")
    assert outside.exists()


@pytest.mark.skipif(not git_available(), reason="git not installed")
def test_delete_path_is_one_commit_and_dependency_folders_are_not_committed(tmp_path):
    s = ArtifactStore(tmp_path / "art")
    s.git = GitRepo(GitConfig())
    s.write_file("r", "lib/a.py", "a\n", "t1", "coder")
    s.write_file("r", "lib/b.py", "b\n", "t1", "coder")
    ws = s.workspace("r")
    assert "node_modules/" in (ws / ".git" / "info" / "exclude").read_text()
    _node_modules(ws)
    s.final_commit("r", "snapshot", "coder")  # commits "." : node_modules stays out of git
    tracked = subprocess.run(["git", "ls-files"], cwd=ws, capture_output=True, text=True, check=True).stdout
    assert "node_modules" not in tracked
    rec = s.delete_path("r", "node_modules", "t2", "coder")  # untracked: deleted, no commit, no error
    assert "git_error" not in rec and "commit" not in rec
    rec = s.delete_path("r", "lib", "t2", "coder")
    assert rec["commit"] and "git_error" not in rec
    out = subprocess.run(["git", "show", "--stat", "--format=%s", "HEAD"], cwd=ws,
                         capture_output=True, text=True, check=True).stdout
    assert out.startswith("delete lib/ (2 files) by coder (t2)") and "lib/a.py" in out and "lib/b.py" in out


def test_run_copies_skip_caches_but_keep_node_modules():
    assert ".git" in RUN_COPY_SKIP and "__pycache__" in RUN_COPY_SKIP
    assert "node_modules" not in RUN_COPY_SKIP  # runs are offline: an installed node_modules must reach them


def test_delete_path_tool_respects_write_paths(tmp_path):
    b = build(ROOT / "agents.json", tmp_path / "art", str(tmp_path / "logs" / "ev.jsonl"), mock=True)
    task = Task(prompt="p", assignee="planner")

    def call(agent, **inp):
        return asyncio.run(b.agents[agent]._tool(task, NS(id="c1", name="delete_path", input=inp)))

    b.artifacts.write_file(task.root_id, "plans/a.md", "# A\n", task.id, "planner")
    b.artifacts.write_file(task.root_id, "src/app.py", "x = 1\n", task.id, "coder")
    _node_modules(b.artifacts.workspace(task.root_id))
    r = call("planner", path="src")
    assert r.get("is_error") and "not deleted" in r["content"]
    r = call("coder", path="node_modules")
    assert not r.get("is_error") and r["content"].startswith("deleted folder node_modules/ (3 files)")
    r = call("planner", path="plans/a.md")
    assert not r.get("is_error") and r["content"].startswith("deleted plans/a.md")
    assert {f["file"] for f in b.artifacts.list_files(task.root_id)} == {"src/app.py"}
