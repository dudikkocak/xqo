"""Peering and the master config with the Claude Agent SDK backend: the peer tools reach Claude Code through the
in-process xqo server, files Claude Code writes travel to the peer, and drains / backups hold peer
traffic instead of losing it."""
import asyncio
import json
from pathlib import Path

from fastapi.testclient import TestClient

from xqo import backup
from xqo.web import create_app
from xqo_peering import create_peer_app

from test_peers import make, settle
import pytest

test_sdk_backend = pytest.importorskip("test_sdk_backend", reason="needs the base package's tests (monorepo checkout)")
fake, make_bus = test_sdk_backend.fake, test_sdk_backend.make_bus  # fake is a fixture

ROOT = Path(__file__).resolve().parent


def test_peer_tools_run_in_claude_code(fake, tmp_path):  # noqa: F811
    ex = str(tmp_path / "exchange")
    a = make_bus(tmp_path / "alpha", [{"name": "planner", "skills": ["planning"]}],
                 peering={"name": "alpha", "exchange_dir": ex, "peers": {"beta": {"description": "marketing org"}}})
    b = make(tmp_path, "beta", {"exchange_dir": ex, "peers": {"alpha": {}}})
    planner = a.agents["planner"]
    assert planner.provider_type == "claude-sdk"
    fake.rounds = [[
        ("tool", "mcp__xqo__list_peers", {}),
        ("tool", "Write", {"file_path": "docs/brief.md", "content": "# Brief\nlaunch\n"}),
        ("tool", "mcp__xqo__send_to_peer", {"peer": "beta", "message": "Please market this",
                                                     "files": ["docs/brief.md"]}),
        ("text", "Sent."),
    ]]
    t = asyncio.run(a.submit("Hand the brief to marketing", agent="planner"))
    assert t.status == "done", t.result
    o = fake.runs[0]
    assert {"list_peers", "send_to_peer"} <= set(o.mcp_servers["xqo"]["tools"])
    assert "beta: may send files and messages (marketing org)" in o.system_prompt
    listed = next(c for c in fake.calls if c["tool"].endswith("list_peers"))
    assert "beta" in listed["result"] and not listed["is_error"]
    sent = next(c for c in fake.calls if c["tool"].endswith("send_to_peer"))
    assert sent["result"].startswith("delivered to beta"), sent

    async def receive():
        assert await b.peers.poll_exchange() == 1
        await settle(b)
    asyncio.run(receive())
    bt = b.tasks[b.peers.messages()[-1]["task"]]
    assert b.artifacts.read_file(bt.id, "peers/alpha/docs/brief.md") == "# Brief\nlaunch\n"


def test_drain_holds_peer_messages(tmp_path):
    ex = str(tmp_path / "exchange")
    a = make(tmp_path, "alpha", {"exchange_dir": ex, "peers": {"beta": {}}})
    b = make(tmp_path, "beta", {"exchange_dir": ex, "peers": {"alpha": {}}})

    async def go():
        t = await a.create("origin", "planner")
        await a.peers.send("beta", "hello", task=t, agent="planner")
        b.draining = True
        assert await b.peers.poll_exchange() == 0  # left in the inbox, not rejected
        assert list((tmp_path / "exchange" / "beta" / "inbox").glob("*.json"))
        b.draining = False
        assert await b.peers.poll_exchange() == 1
        await settle(b)
    asyncio.run(go())
    assert not (tmp_path / "exchange" / "beta" / "rejected").exists()


def test_http_inbox_answers_503_while_draining(tmp_path, monkeypatch):
    monkeypatch.setenv("TOK", "s3cret")
    b = make(tmp_path, "beta", {"listen": "127.0.0.1:0", "peers": {"alpha": {"url": "http://x", "token_env": "TOK"}}})
    b.draining = True
    env = {"v": 1, "id": "pmsg_1", "from": "alpha", "to": "beta", "kind": "message", "text": "hi", "files": [], "hops": 0}
    r = TestClient(create_peer_app(b)).post("/peer/v1/inbox", json=env,
                                           headers={"X-XQO-Peer": "alpha", "Authorization": "Bearer s3cret"})
    assert r.status_code == 503 and "draining" in r.json()["detail"]
    assert "pmsg_1" not in b.peers.seen  # the sender's retry is accepted later


def test_state_backup_keeps_peering_state(tmp_path):
    ex = str(tmp_path / "exchange")
    a = make(tmp_path, "alpha", {"exchange_dir": ex, "peers": {"beta": {}}})

    async def go():
        t = await a.create("origin", "planner")
        await a.peers.send("beta", "hello", task=t, agent="planner")
    asyncio.run(go())
    files = backup.data_files(a, "state", tmp_path / "alpha")
    assert any("artifacts/.peers/messages.jsonl" in str(f) for f in files)


def test_limits_warnings_join_grant_warnings_in_health(tmp_path):
    cfg = json.loads((ROOT / "agents.json").read_text())
    cfg["limits"] = {"image_mb": 8}
    cfg["git"] = {"enabled": False}
    (tmp_path / "agents.json").write_text(json.dumps(cfg))
    from xqo.config import build
    bus = build(tmp_path / "agents.json", tmp_path / "art", str(tmp_path / "ev.jsonl"), mock=True,
                departments="none", departments_dir=ROOT / "departments")
    assert bus.limits.config["image_mb"] == 8
    with TestClient(create_app(bus)) as c:
        h = c.get("/api/health").json()
        assert any("limits.image_mb" in w for w in h["warnings"])
