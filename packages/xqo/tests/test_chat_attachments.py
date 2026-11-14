"""Attachments on Ask / Steer messages: stored with the task, sent to the model, kept across restarts."""
import asyncio
import base64

import pytest
from fastapi.testclient import TestClient

from xqo.attachments import validate
from xqo.web import create_app
from test_attachments import PDF, png
from test_chat import busy, hold, kinds, make, root_of, until


def spy(b):
    """Record every content block list the model is sent, per agent."""
    seen = []
    llm = b.agents["planner"].llm
    orig = llm.create

    async def rec(**kw):
        for m in kw["messages"]:
            if m["role"] == "user" and isinstance(m["content"], list):
                seen.append((kw["agent"], [blk for blk in m["content"] if isinstance(blk, dict)]))
        return await orig(**kw)
    llm.create = rec
    return seen


def has(seen, agent, kind, text):
    return any(a == agent and any(blk.get("type") == kind for blk in blocks)
               and any(text in blk.get("text", "") for blk in blocks if blk.get("type") == "text")
               for a, blocks in seen)


def test_ask_with_files_reaches_the_agent_and_persists(tmp_path):
    b = make(tmp_path)
    seen = spy(b)

    async def go():
        gate, _ = hold(b, "reviewer")
        job = asyncio.create_task(b.submit("Build fib"))
        await until(lambda: busy(b, "reviewer"))
        root = root_of(b)
        files = validate([("shot.png", None, png()), ("spec.pdf", None, PDF)])
        r = await b.chat.send(root.id, "Does this match?", "ask", to="reviewer", files=files)
        assert [a["name"] for a in r["attachments"]] == ["shot.png", "spec.pdf"]
        gate.set()
        return await job

    t = asyncio.run(go())
    assert t.status == "done"
    assert has(seen, "reviewer", "image", "Attached above: shot.png (image")
    assert has(seen, "reviewer", "document", "Does this match?")
    assert [a["name"] for a in t.attachments] == ["shot.png", "spec.pdf"]  # agents can forward them
    msg = kinds(b, t.id, "user_message")[0]
    assert [a["name"] for a in msg["meta"]["attachments"]] == ["shot.png", "spec.pdf"]
    assert b.attachments.path(t.id, "shot.png").read_bytes() == png()
    b2 = make(tmp_path)  # the files are still listed on the task after a restart
    assert [a["name"] for a in b2.tasks[t.id].attachments] == ["shot.png", "spec.pdf"]


def test_steer_with_only_a_file_reopens_a_finished_task(tmp_path):
    b = make(tmp_path)
    t = asyncio.run(b.submit("Build fib"))
    seen = spy(b)

    async def go():
        r = await b.chat.send(t.id, "", "steer", files=validate([("notes.md", None, b"# use memoization")]))
        assert r["reopened"]
        await until(lambda: not b.is_running(t.id) and t.status == "done")
    asyncio.run(go())
    assert has(seen, "planner", "document", "(no text, see the attachments)")
    assert any(blk.get("source", {}).get("data") == "# use memoization" for _, bl in seen for blk in bl)


def test_files_with_an_answer_follow_as_a_note(tmp_path):
    b = make(tmp_path)
    seen = spy(b)

    async def go():
        job = asyncio.create_task(b.submit("Please confirm, then build fib"))
        await until(lambda: b.attention.pending())
        r = await b.chat.send(root_of(b).id, "like this", "ask", files=validate([("plan.png", None, png())]))
        assert r["answered_attention"]
        return await job
    asyncio.run(go())
    att = next(iter(b.attention.items.values()))
    assert att.answer.startswith("like this\n\n(attached: plan.png")
    assert has(seen, "planner", "image", "attached these files to their answer")


def test_urgent_ask_side_reply_sees_the_file(tmp_path):
    b = make(tmp_path)
    seen = spy(b)

    async def go():
        gate, _ = hold(b, "reviewer")
        job = asyncio.create_task(b.submit("Build fib"))
        await until(lambda: busy(b, "reviewer"))
        root = root_of(b)
        await b.chat.send(root.id, "What is this?", "ask", urgent=True, files=validate([("x.png", None, png())]))
        await until(lambda: kinds(b, root.id, "agent_reply"))
        gate.set()
        return await job
    asyncio.run(go())
    assert has(seen, "planner", "image", "URGENT message from the user")


def test_bad_chat_attachments(tmp_path):
    b = make(tmp_path)
    t = asyncio.run(b.submit("Build fib"))

    async def go():
        with pytest.raises(KeyError):  # nothing is stored for a message that is refused
            await b.chat.send(t.id, "hi", "ask", to="nobody", files=validate([("a.txt", None, b"x")]))
    asyncio.run(go())
    assert b.attachments.path(t.id, "a.txt") is None

    with TestClient(create_app(b)) as c:
        url = f"/api/tasks/{t.id}/chat"
        assert c.post(url, json={"text": " "}).status_code == 422
        bad = {"name": "a.zip", "media_type": "application/zip", "data": base64.b64encode(b"PK\x03\x04\x00bin").decode()}
        assert c.post(url, json={"text": "x", "attachments": [bad]}).status_code == 400
        assert c.post(url, json={"text": "x", "attachments": [{"name": "a.txt", "data": "%%%"}]}).status_code == 400
        ok = {"name": "shot.png", "media_type": "image/png", "data": base64.b64encode(png()).decode()}
        r = c.post(url, json={"kind": "ask", "attachments": [ok]}).json()
        assert r["reopened"] and r["attachments"][0]["name"] == "shot.png"
        c.portal.call(lambda: until(lambda: not b.is_running(t.id)))
        assert c.get(f"/api/attachments/{t.id}/shot.png").content == png()
