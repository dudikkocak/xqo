"""move_file: agents rename workspace files with a tool (no shell or git mv needed); rename diffs drop the old path."""
import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from xqo.artifacts import ArtifactStore
from xqo.config import build
from xqo.gitstore import GitConfig, GitRepo, git_available
from xqo.models import Task
from xqo.patching import PatchError

ROOT = Path(__file__).resolve().parent.parent


def test_move_file_moves_content_and_records_both_paths(tmp_path):
    s = ArtifactStore(tmp_path / "art")
    s.write_file("r", "src/old.py", "x = 1\n", "t1", "coder")
    recs = s.move_file("r", "src/old.py", "pkg/new.py", "t2", "coder")
    assert [(r["action"], r["file"]) for r in recs] == [("move", "pkg/new.py"), ("delete", "src/old.py")]
    assert {f["file"] for f in s.list_files("r")} == {"pkg/new.py"}
    assert s.read_file("r", "pkg/new.py") == "x = 1\n"
    assert s.state_from(s.list("t1") + s.list("t2")) == {"pkg/new.py": "x = 1\n"}


@pytest.mark.parametrize("src,dst,msg", [
    ("missing.py", "b.py", "not in workspace"), ("a.py", "b.py", "already exists"),
    ("a.py", "./a.py", "already at that path"), ("a.py", ".git/config", ".git folder"),
])
def test_move_file_refusals(tmp_path, src, dst, msg):
    s = ArtifactStore(tmp_path / "art")
    s.write_file("r", "a.py", "a\n", "t1", "coder")
    s.write_file("r", "b.py", "b\n", "t1", "coder")
    with pytest.raises(PatchError, match=msg):
        s.move_file("r", src, dst, "t1", "coder")
    assert s.read_file("r", "a.py") == "a\n" and s.read_file("r", "b.py") == "b\n"


@pytest.mark.skipif(not git_available(), reason="git not installed")
def test_move_file_is_one_git_rename(tmp_path):
    s = ArtifactStore(tmp_path / "art")
    s.git = GitRepo(GitConfig())
    s.write_file("r", "src/old.py", "def f():\n    return 1\n", "t1", "coder")
    recs = s.move_file("r", "src/old.py", "src/new.py", "t2", "coder")
    assert recs[0]["commit"] == recs[1]["commit"]
    out = subprocess.run(["git", "show", "--stat", "-M", "--format=%s", "HEAD"], cwd=s.workspace("r"),
                         capture_output=True, text=True, check=True).stdout
    assert out.startswith("move src/old.py -> src/new.py by coder (t2)")
    assert "src/{old.py => new.py}" in out


def test_rename_diff_drops_old_path(tmp_path):
    s = ArtifactStore(tmp_path / "art")
    s.write_file("r", "a.py", "x = 1\n", "t1", "coder")
    diff = "--- a/a.py\n+++ b/b.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
    recs = s.apply_diff("r", diff, "t2", "coder")
    assert [(r["action"], r["file"]) for r in recs] == [("patch", "b.py"), ("delete", "a.py")]
    assert {f["file"] for f in s.list_files("r")} == {"b.py"}


def test_move_file_tool_respects_write_paths(tmp_path):
    b = build(ROOT / "agents.json", tmp_path / "art", str(tmp_path / "logs" / "ev.jsonl"), mock=True)
    task = Task(prompt="p", assignee="planner")

    def call(agent, **inp):
        return asyncio.run(b.agents[agent]._tool(task, NS(id="c1", name="move_file", input=inp)))

    b.artifacts.write_file(task.root_id, "plans/a.md", "# A\n", task.id, "planner")
    b.artifacts.write_file(task.root_id, "src/app.py", "x = 1\n", task.id, "coder")
    r = call("planner", src="src/app.py", dst="plans/app.md")
    assert r.get("is_error") and "not moved" in r["content"]
    r = call("planner", src="plans/a.md", dst="plans/b.md")
    assert not r.get("is_error") and r["content"].startswith("moved plans/a.md -> plans/b.md")
    r = call("coder", src="src/app.py", dst="src/main.py")
    assert not r.get("is_error")
    assert {f["file"] for f in b.artifacts.list_files(task.root_id)} == {"plans/b.md", "src/main.py"}
