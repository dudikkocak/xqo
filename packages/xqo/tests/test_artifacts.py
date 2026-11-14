"""File-name detection for fenced blocks, workspace layout and patching."""
import asyncio
from pathlib import Path

import pytest

from xqo.artifacts import ArtifactStore
from xqo.config import build
from xqo.fences import normalize_path, parse_fences
from xqo.patching import PatchError

ROOT = Path(__file__).resolve().parent.parent


def one(text):
    fs = parse_fences(text)
    assert len(fs) == 1, fs
    return fs[0]


@pytest.mark.parametrize("text,path,lang", [
    ("```python src/app.py\nx=1\n```", "src/app.py", "python"),
    ("```src/app.py\nx=1\n```", "src/app.py", "python"),
    ("```python:src/app.py\nx=1\n```", "src/app.py", "python"),
    ("```python|src/app.py\nx=1\n```", "src/app.py", "python"),
    ('```python title="src/app.py"\nx=1\n```', "src/app.py", "python"),
    ("```python filename=src/app.py\nx=1\n```", "src/app.py", "python"),
    ("```ts file='web/main.ts'\nx\n```", "web/main.ts", "ts"),
    ('```{.python file="src/app.py"}\nx=1\n```', "src/app.py", "python"),
    ('```{.js title="a/b.js" linenos=true}\nx\n```', "a/b.js", "js"),
    ("```python (src/app.py)\nx=1\n```", "src/app.py", "python"),
    ("```python [src/app.py]\nx=1\n```", "src/app.py", "python"),
    ("**src/app.py**\n```python\nx=1\n```", "src/app.py", "python"),
    ("`src/app.py`:\n```python\nx=1\n```", "src/app.py", "python"),
    ("### src/app.py\n\n```python\nx=1\n```", "src/app.py", "python"),
    ("#### `src/app.py`\n```python\nx=1\n```", "src/app.py", "python"),
    ("File: src/app.py\n```python\nx=1\n```", "src/app.py", "python"),
    ("Filename: `src/app.py`\n```python\nx=1\n```", "src/app.py", "python"),
    ("**File:** `src/app.py`\n```python\nx=1\n```", "src/app.py", "python"),
    ("Path - src/app.py\n```python\nx=1\n```", "src/app.py", "python"),
    ("src/app.py:\n```python\nx=1\n```", "src/app.py", "python"),
    ("1. **src/app.py** (new)\n```python\nx=1\n```", "src/app.py", "python"),
    ("Create `src/app.py` with the following:\n```python\nx=1\n```", "src/app.py", "python"),
    ("```python\n# file: src/app.py\nx=1\n```", "src/app.py", "python"),
    ("```python\n# src/app.py\nx=1\n```", "src/app.py", "python"),
    ("```ts\n// filename: src/main.ts\nx\n```", "src/main.ts", "ts"),
    ("```html\n<!-- file: public/index.html -->\n<p/>\n```", "public/index.html", "html"),
    ("```css\n/* path: styles/site.css */\na{}\n```", "styles/site.css", "css"),
    ("```sql\n-- file: db/schema.sql\nselect 1;\n```", "db/schema.sql", "sql"),
    ("```dockerfile Dockerfile\nFROM python\n```", "Dockerfile", "dockerfile"),
    ("```.gitignore\n*.pyc\n```", ".gitignore", "gitignore"),
    ("```bash ./scripts/run.sh\necho hi\n```", "scripts/run.sh", "bash"),
    ("```python C:\\proj\\src\\app.py\nx=1\n```", "proj/src/app.py", "python"),
    ("~~~python src/app.py\nx=1\n~~~", "src/app.py", "python"),
    ("````markdown docs/README.md\n```py\ninner\n```\n````", "docs/README.md", "markdown"),
    ("- item\n   ```python src/app.py\n   x=1\n   ```", "src/app.py", "python"),
])
def test_filename_variants(text, path, lang):
    f = one(text)
    assert (f.path, f.lang) == (path, lang)
    assert f.closed


@pytest.mark.parametrize("text", [
    "```python\nprint('hi')\n```",
    "```bash\npip install x\n```",
    "Run this:\n```bash\nls\n```",
    "Version 1.2.3\n```text\nhello\n```",
    "See https://example.com/a.py\n```python\nx=1\n```",
    "```python\n# compute the value\nx=1\n```",
    "```json\n{}\n```",
    "```Dockerfile\nFROM python\n```",  # bare language tag, not a file name
])
def test_unnamed_blocks(text):
    assert one(text).path is None


def test_nested_fence_content_preserved():
    f = one("````md README.md\n```py\nx\n```\n````")
    assert f.code == "```py\nx\n```\n"


def test_unclosed_fence_is_truncated():
    f = one("```python src/a.py\nx = 1")
    assert not f.closed


@pytest.mark.parametrize("bad", ["../etc/passwd", "a/../../x", "/../x", "http://x.com/a.py", ""])
def test_unsafe_paths_rejected(bad):
    assert normalize_path(bad) is None


def test_absolute_path_made_relative():
    assert normalize_path("/home/u/app.py") == "home/u/app.py"


# ---------------------------------------------------------------- workspace
@pytest.fixture
def store(tmp_path):
    return ArtifactStore(tmp_path / "art")


def run(store, text, agent="coder", task="t1", root="R"):
    return store.process_text(text, task, agent, root)


def test_named_files_land_in_workspace_with_history(store):
    recs, errs = run(store, "```python src/pkg/app.py\nv = 1\n```\n**README.md**\n```md\n# hi\n```\n```bash\nls\n```")
    assert not errs
    assert [r["action"] for r in recs] == ["create", "create", "snippet"]
    assert store.read_file("R", "src/pkg/app.py") == "v = 1\n"
    assert {f["file"] for f in store.list_files("R")} == {"src/pkg/app.py", "README.md"}
    assert recs[0]["path"].startswith("R/coder/t1/") and recs[0]["path"].endswith("/src/pkg/app.py")
    run(store, "```python src/pkg/app.py\nv = 2\n```", agent="reviewer", task="t2")
    hist = store.history("R", "src/pkg/app.py")
    assert [h["action"] for h in hist] == ["create", "update"]
    assert store.read_file("R", "src/pkg/app.py") == "v = 2\n"
    assert (store.root / hist[0]["path"]).read_text() == "v = 1\n"  # old version kept


BASE = "def f(x):\n    return x\n\n\ndef g():\n    pass\n"


def test_unified_diff_patch(store):
    run(store, f"```python src/m.py\n{BASE}```")
    diff = ("```diff\n--- a/src/m.py\n+++ b/src/m.py\n@@ -1,2 +1,3 @@\n def f(x):\n-    return x\n"
            "+    y = x * 2\n+    return y\n```")
    recs, errs = run(store, diff, agent="reviewer")
    assert not errs and recs[0]["action"] == "patch" and recs[0]["patch_path"].endswith(".diff")
    assert store.read_file("R", "src/m.py").startswith("def f(x):\n    y = x * 2\n    return y\n\n")


def test_diff_with_wrong_line_numbers_and_no_numbers(store):
    run(store, f"```python src/m.py\n{BASE}```")
    _, errs = run(store, "```diff\n--- src/m.py\n+++ src/m.py\n@@ -40,2 +40,2 @@\n def g():\n-    pass\n+    return 1\n```")
    assert not errs
    _, errs = run(store, "```diff src/m.py\n@@ ... @@\n def g():\n-    return 1\n+    return 2\n```")
    assert not errs
    assert store.read_file("R", "src/m.py").endswith("def g():\n    return 2\n")


def test_multi_file_diff_new_and_delete(store):
    run(store, "```python a.py\nA\n```\n```python b.py\nB\n```")
    diff = ("```diff\ndiff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-A\n+A2\n"
            "--- /dev/null\n+++ b/new/c.py\n@@ -0,0 +1,2 @@\n+C1\n+C2\n"
            "--- a/b.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-B\n```")
    recs, errs = run(store, diff)
    assert not errs
    assert [r["action"] for r in recs] == ["patch", "patch", "delete"]
    assert store.read_file("R", "a.py") == "A2\n" and store.read_file("R", "new/c.py") == "C1\nC2\n"
    assert {f["file"] for f in store.list_files("R")} == {"a.py", "new/c.py"}


def test_failed_diff_is_atomic_and_reported(store):
    run(store, "```python a.py\nA\n```\n```python b.py\nB\n```")
    diff = ("```diff\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-A\n+A2\n"
            "--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-NOPE\n+B2\n```")
    recs, errs = run(store, diff)
    assert len(errs) == 1 and "b.py" in errs[0]
    assert recs[0]["action"] == "patch_failed"
    assert store.read_file("R", "a.py") == "A\n"  # nothing applied


def test_search_replace_blocks(store):
    run(store, f"```python src/m.py\n{BASE}```")
    sr = ("```python src/m.py\n<<<<<<< SEARCH\n    return x\n=======\n    return x + 1\n>>>>>>> REPLACE\n```\n"
          "```\nsrc/m.py\n<<<<<<< SEARCH\n    pass\n=======\n    return 0\n>>>>>>> REPLACE\n```")
    recs, errs = run(store, sr)
    assert not errs and [r["action"] for r in recs] == ["patch", "patch"]
    out = store.read_file("R", "src/m.py")
    assert "return x + 1" in out and "return 0" in out


def test_search_replace_ambiguous_fails(store):
    run(store, "```python m.py\nx = 1\nx = 1\n```")
    _, errs = run(store, "```python m.py\n<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n>>>>>>> REPLACE\n```")
    assert errs and "2 times" in errs[0]


def test_patch_file_tool_edits(store):
    run(store, f"```python src/m.py\n{BASE}```")
    recs = store.patch_file("R", "src/m.py", "t9", "reviewer", edits=[{"old": "pass", "new": "return 5"}])
    assert recs[0]["action"] == "patch" and "return 5" in store.read_file("R", "src/m.py")
    with pytest.raises(PatchError):
        store.patch_file("R", "src/m.py", "t9", "reviewer", edits=[{"old": "missing", "new": "x"}])
    with pytest.raises(PatchError):
        store.patch_file("R", "nope.py", "t9", "reviewer", edits=[{"old": "a", "new": "b"}])


def test_truncated_named_block_does_not_touch_workspace(store):
    run(store, "```python a.py\ngood\n```")
    recs, _ = run(store, "```python a.py\nhalf")
    assert recs[0]["action"] == "truncated" and store.read_file("R", "a.py") == "good\n"


def test_zip_contains_workspace_tree(store):
    import io
    import zipfile
    run(store, "```python src/a.py\nA\n```")
    names = zipfile.ZipFile(io.BytesIO(store.zip_task("R"))).namelist()
    assert "workspace/src/a.py" in names and any(n.startswith("history/") for n in names)
    assert zipfile.ZipFile(io.BytesIO(store.zip_task("R", workspace_only=True))).namelist() == ["src/a.py"]


# --------------------------------------------------- agent-level integration
def test_mock_flow_builds_and_patches_workspace(tmp_path):
    bus = build(ROOT / "agents.json", tmp_path / "art", str(tmp_path / "ev.jsonl"), mock=True)
    task = asyncio.run(bus.submit("Build fib"))
    files = {f["file"] for f in bus.artifacts.list_files(task.id)}
    assert {"src/fib.py", "tests/test_fib.py"} <= files
    src = bus.artifacts.read_file(task.id, "src/fib.py")
    assert "raise ValueError" in src  # reviewer's diff applied to coder's file
    acts = [h["action"] for h in bus.artifacts.history(task.id, "src/fib.py")]
    assert acts == ["create", "patch"]


def test_patch_failure_is_fed_back_to_model(tmp_path):
    from types import SimpleNamespace as NS

    from xqo.agent import Agent
    from xqo.bus import MessageBus

    seen = []

    class LLM:
        async def create(self, *, messages, **_):
            turn = sum(1 for m in messages if m["role"] == "assistant")
            seen.append(messages[-1]["content"])
            if turn == 0:
                t = "```python a.py\nx = 1\n```\n```diff\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 9\n+x = 2\n```"
            else:
                t = "```diff\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n```"
            return NS(content=[NS(type="text", text=t)], stop_reason="end_turn", usage=None, model="m")

    store = ArtifactStore(tmp_path / "art")
    bus = MessageBus(tmp_path / "ev.jsonl")
    bus.artifacts = store
    bus.register(Agent("a", ["x"], "sys", LLM(), store))
    task = asyncio.run(bus.submit("go"))
    assert "patches failed" in seen[1]
    assert store.read_file(task.id, "a.py") == "x = 2\n"


def test_source_zip_leaves_out_dependency_and_build_folders(store):
    import io
    import zipfile
    run(store, "```python src/a.py\nA\n```")
    ws = store.workspace("R")
    junk = ["node_modules/x/index.js", "web/node_modules/y.js", ".venv/lib/z.py", "env1/pyvenv.cfg", "env1/lib/q.py",
            "src/__pycache__/a.cpython-312.pyc", "dist/app.js", "build/out.o", "rs/target/debug/app", "go/vendor/m/m.go",
            "cs/bin/Debug/app.dll", "cs/obj/x.json"]
    keep = ["rs/Cargo.toml", "rs/src/main.rs", "go/go.mod", "cs/App.csproj", "tools/bin/run.sh", "lib/vendor/v.js",
            "package.json"]
    for rel in junk + keep:
        (ws / rel).parent.mkdir(parents=True, exist_ok=True)
        (ws / rel).write_text("x", encoding="utf-8")
    names = set(zipfile.ZipFile(io.BytesIO(store.zip_task("R", source_only=True))).namelist())
    assert names == {"src/a.py", *keep}
    full = set(zipfile.ZipFile(io.BytesIO(store.zip_task("R", workspace_only=True))).namelist())
    assert set(junk) <= full  # the workspace zip is unchanged
