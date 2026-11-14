"""Trim old tool traffic in an agent's conversation to save tokens.

Before each model call, tool results (test runs, file reads, listings, MCP output) and large tool inputs
(file contents sent with write_file / patch_file) that are older than the last `keep_turns` exchanges are
replaced with a short stub: the first lines, the size, and where to get the full text again (the saved run
artifact, or read_file on the workspace file). The task prompt, the agent's own text, chat messages, notes and
errors stay as they are.

Trimming runs in batches: only when at least `batch_chars` can be saved, so the cached prompt prefix stays
valid between batches instead of changing every turn.

agents.json:  "compaction": {"enabled": true, "keep_turns": 2, "min_chars": 1500, "batch_chars": 20000}
XQO_COMPACT=0 turns it off.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

STUB_MARK = "[trimmed to save tokens:"
HEAD_CHARS = 300


@dataclass
class CompactionConfig:
    enabled: bool = True
    keep_turns: int = 2  # newest assistant/tool-result exchanges kept in full
    min_chars: int = 1500  # smaller blocks are never trimmed
    batch_chars: int = 20000  # trim only when this much can be saved at once

    @classmethod
    def from_dict(cls, d: dict | None) -> CompactionConfig:
        d = dict(d or {})
        c = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        if os.getenv("XQO_COMPACT") == "0":
            c.enabled = False
        if c.keep_turns < 1 or c.min_chars < 200 or c.batch_chars < 0:
            raise ValueError("compaction: keep_turns >= 1, min_chars >= 200, batch_chars >= 0")
        return c


def _get(b, k, default=None):
    return b.get(k, default) if isinstance(b, dict) else getattr(b, k, default)


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_get(x, "text", "") or "" for x in content if _get(x, "type") == "text")
    return ""


def _head(text: str) -> str:
    lines = text[:HEAD_CHARS].splitlines()[:4]
    return "\n".join(lines)


def _result_stub(text: str, call: dict | None, hint: str | None) -> str:
    name = (call or {}).get("name", "tool")
    inp = (call or {}).get("input") or {}
    where = hint or (f"read_file('{inp['path']}') to see it again" if name == "read_file" and inp.get("path")
                     else f"call {name} again if you need it")
    return f"{_head(text)}\n{STUB_MARK} {len(text)} chars of {name} output; {where}]"


def compact(messages: list, cfg: CompactionConfig, hints: dict | None = None) -> tuple[int, int]:
    """Trim old tool results / inputs in place. Returns (blocks trimmed, chars saved)."""
    if not cfg.enabled:
        return 0, 0
    hints = hints or {}
    # exchanges = assistant turns that called tools; keep the newest `keep_turns` of them and their results
    tool_turns = [i for i, m in enumerate(messages) if m["role"] == "assistant" and isinstance(m["content"], list)
                  and any(_get(b, "type") == "tool_use" for b in m["content"])]
    if len(tool_turns) <= cfg.keep_turns:
        return 0, 0
    cutoff = tool_turns[-cfg.keep_turns]  # messages before this index are "old"
    calls: dict[str, dict] = {}
    plan: list[tuple[int, int, object]] = []  # (message index, block index, replacement)
    saved = 0
    for i, m in enumerate(messages[:cutoff]):
        if not isinstance(m["content"], list):
            continue
        for j, b in enumerate(m["content"]):
            t = _get(b, "type")
            if t == "tool_use":
                inp = dict(_get(b, "input") or {})
                calls[_get(b, "id")] = {"name": _get(b, "name"), "input": inp}
                big = {k: v for k, v in inp.items() if isinstance(v, str) and len(v) >= cfg.min_chars
                       and not v.startswith(STUB_MARK)}
                if big:
                    new = {**inp, **{k: f"{STUB_MARK} {len(v)} chars sent"
                                        + (f"; read_file('{inp['path']}') shows the current file" if inp.get("path")
                                           else "") + "]" for k, v in big.items()}}
                    saved += sum(len(v) for v in big.values()) - sum(len(new[k]) for k in big)
                    plan.append((i, j, {"type": "tool_use", "id": _get(b, "id"), "name": _get(b, "name"),
                                        "input": new}))
            elif t == "tool_result":
                text = _text_of(_get(b, "content"))
                if len(text) < cfg.min_chars or STUB_MARK in text:
                    continue
                if _get(b, "is_error"):
                    continue  # errors are short and worth keeping
                tid = _get(b, "tool_use_id")
                stub = _result_stub(text, calls.get(tid), hints.get(tid))
                saved += len(text) - len(stub)
                plan.append((i, j, {"type": "tool_result", "tool_use_id": tid, "content": stub}))
    if not plan or saved < cfg.batch_chars:
        return 0, 0
    for i, j, new in plan:
        content = list(messages[i]["content"])
        content[j] = new
        messages[i] = {**messages[i], "content": content}
    return len(plan), saved
