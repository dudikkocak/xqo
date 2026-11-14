"""Trimming old tool results and large tool inputs from an agent's history."""
import pytest

from xqo.compaction import STUB_MARK, CompactionConfig, compact


def convo(turns=4, size=8000):
    msgs = [{"role": "user", "content": "Build fib and test it"}]
    for i in range(turns):
        msgs.append({"role": "assistant", "content": [
            {"type": "text", "text": f"step {i}"},
            {"type": "tool_use", "id": f"w{i}", "name": "write_file", "input": {"path": f"src/m{i}.py", "content": "x" * size}},
            {"type": "tool_use", "id": f"r{i}", "name": "run_code", "input": {"command": "pytest -q"}}]})
        msgs.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"w{i}", "content": f"saved src/m{i}.py"},
            {"type": "tool_result", "tool_use_id": f"r{i}", "content": "FAILED test_x\n" + "trace\n" * (size // 6)},
            {"type": "tool_result", "tool_use_id": "err", "content": "E" * size, "is_error": True}]})
    return msgs


def test_trims_old_blocks_and_keeps_recent_ones():
    msgs = convo()
    before = [m["content"] if isinstance(m["content"], str) else list(m["content"]) for m in msgs]
    n, saved = compact(msgs, CompactionConfig(), {"r0": "the full log is saved as run artifact runs/1.txt"})
    assert n == 4 and saved > 20000  # 2 old turns x (write_file input + run output)
    old_ws, old_run = msgs[1]["content"][1], msgs[2]["content"][1]
    assert old_ws["input"]["path"] == "src/m0.py" and old_ws["input"]["content"].startswith(STUB_MARK)
    assert "read_file('src/m0.py')" in old_ws["input"]["content"]
    assert old_run["content"].startswith("FAILED test_x") and "runs/1.txt" in old_run["content"]
    assert msgs[2]["content"][2]["content"] == "E" * 8000  # errors are kept
    assert msgs[0]["content"] == before[0] and msgs[1]["content"][0]["text"] == "step 0"
    for k in range(5, 9):  # the newest two exchanges are untouched
        assert msgs[k]["content"] == before[k]
    assert compact(msgs, CompactionConfig()) == (0, 0)  # already trimmed: nothing more to do


def test_batches_and_settings(monkeypatch):
    assert compact(convo(turns=2), CompactionConfig()) == (0, 0)  # nothing old yet
    assert compact(convo(turns=3, size=2000), CompactionConfig()) == (0, 0)  # too little to save in one batch
    assert compact(convo(turns=3, size=2000), CompactionConfig(batch_chars=0))[0] == 2
    assert compact(convo(), CompactionConfig(enabled=False)) == (0, 0)
    monkeypatch.setenv("XQO_COMPACT", "0")
    assert not CompactionConfig.from_dict({"enabled": True}).enabled
    with pytest.raises(ValueError):
        CompactionConfig.from_dict({"keep_turns": 0})


def test_agent_trims_during_a_task(tmp_path):
    import asyncio
    from pathlib import Path
    from types import SimpleNamespace as NS

    from xqo.config import build

    root = Path(__file__).resolve().parent.parent
    b = build(root / "agents.json", tmp_path / "a", str(tmp_path / "e.jsonl"), mock=True)
    b.compaction = CompactionConfig(keep_turns=1, batch_chars=0)
    name = next(n for n, a in b.agents.items() if "planning" not in [s.lower() for s in a.skills])
    agent, seen = b.agents[name], []

    async def create(*, messages, **_):  # writes a big file three times, then finishes
        seen.append(sum(len(str(m["content"])) for m in messages))
        k = sum(1 for m in messages if m["role"] == "assistant")
        if k < 3:
            return NS(content=[NS(type="tool_use", id=f"t{k}", name="write_file",
                                  input={"path": f"src/f{k}.py", "content": "y" * 6000})],
                      stop_reason="tool_use", usage=NS(input_tokens=1, output_tokens=1), model="m")
        return NS(content=[NS(type="text", text="done")], stop_reason="end_turn",
                  usage=NS(input_tokens=1, output_tokens=1), model="m")

    agent.llm = NS(create=create)
    t = asyncio.run(b.submit("write files", agent=name))
    assert t.status == "done" and agent.compacted["blocks"] >= 1 and agent.compacted["chars"] > 5000
    assert seen[-1] < 3 * 6000  # the history no longer carries every file in full
    assert (b.artifacts.workspace(t.id) / "src" / "f0.py").read_text() == "y" * 6000  # files are untouched
    assert any(e["kind"] == "llm" and (e.get("meta") or {}).get("compacted") for e in b.task_events(t.id))
