"""Peering of separate HQs: exchange folder, HTTP peer API, allowlists, signatures, replies."""
import asyncio
import json
import socket
import threading
import time
from pathlib import Path

import pytest

from xqo.config import build
from xqo_peering.hub import PeerError, sign

ROOT = Path(__file__).resolve().parent  # agents.json and departments/: copies of the base package's examples


def make(tmp_path, name, peering, departments="none", **extra):
    cfg = json.loads((ROOT / "agents.json").read_text())
    cfg["peering"] = {"name": name, **peering}
    cfg["git"] = {"enabled": False}
    cfg.update(extra)
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    (d / "agents.json").write_text(json.dumps(cfg))
    return build(d / "agents.json", d / "artifacts", str(d / "logs" / "events.jsonl"), mock=True,
                 departments=departments, departments_dir=ROOT / "departments")


async def settle(*buses):
    """Wait until every background job of the buses (tasks started by peers, reopened tasks) has ended."""
    for _ in range(200):
        jobs = [j for b in buses for j in b._background if not j.done()]
        if not jobs:
            return
        await asyncio.gather(*jobs, return_exceptions=True)


def pair(tmp_path, a_peer=None, b_peer=None, **kw):
    ex = str(tmp_path / "exchange")
    a = make(tmp_path, "alpha", {"exchange_dir": ex, "peers": {"beta": a_peer or {}}, **kw})
    b = make(tmp_path, "beta", {"exchange_dir": ex, "peers": {"alpha": b_peer or {}}, **kw})
    return a, b


async def task_with_file(bus, path="docs/spec.md", text="# Spec\nhello\n"):
    t = await bus.create("origin task", "planner")
    bus.artifacts.write_file(t.id, path, text, t.id, "planner")
    t.status = "done"
    return t


def test_exchange_message_with_file_and_reply(tmp_path):
    a, b = pair(tmp_path)

    async def go():
        t = await task_with_file(a)
        r = await a.peers.send("beta", "Please review the spec", ["docs/spec.md"], task=t, agent="planner",
                               want_reply=True)
        assert r["status"] == "delivered"
        assert await b.peers.poll_exchange() == 1
        log = b.peers.messages()
        assert log[-1]["status"] == "accepted" and log[-1]["files"] == ["peers/alpha/docs/spec.md"]
        bt = b.tasks[log[-1]["task"]]
        assert b.artifacts.read_file(bt.id, "peers/alpha/docs/spec.md") == "# Spec\nhello\n"
        assert "Please review the spec" in bt.prompt and "sent back" in bt.prompt
        await settle(b)  # b's task ends and its answer goes back to alpha
        assert bt.status == "done"
        assert [m for m in b.peers.messages() if m["dir"] == "out"][-1]["kind"] == "reply"
        assert await a.peers.poll_exchange() == 1
        reply = a.peers.messages()[-1]
        assert reply["kind"] == "reply" and reply["task"] == t.id and reply["detail"] == "reopened the task"
        await settle(a)
        assert any(e["kind"] == "task_reopened" and e["task_id"] == t.id for e in a.history)
        assert (tmp_path / "exchange" / "beta" / "processed").is_dir()

    asyncio.run(go())


def test_duplicates_are_acknowledged_once(tmp_path):
    a, b = pair(tmp_path)

    async def go():
        env = {"v": 1, "id": "pmsg_dup1", "from": "alpha", "to": "beta", "kind": "message", "text": "hi",
               "files": [], "hops": 1}
        first = await b.peers.receive(dict(env), "exchange")
        again = await b.peers.receive(dict(env), "exchange")
        assert first["task_id"] and again["duplicate"]
        await settle(b)

    asyncio.run(go())


def test_allowlists(tmp_path):
    a, b = pair(tmp_path, a_peer={"send": ["messages"]}, b_peer={"receive": ["messages"]})

    async def go():
        t = await task_with_file(a)
        with pytest.raises(PeerError) as e:
            await a.peers.send("beta", "x", ["docs/spec.md"], task=t)
        assert e.value.status == 403
        with pytest.raises(PeerError):
            await a.peers.send("gamma", "x", task=t)
        # alpha may send files to beta's folder by hand, but beta refuses files from alpha
        env = {"v": 1, "id": "pmsg_files1", "from": "alpha", "to": "beta", "kind": "message", "text": "",
               "files": [{"name": "a.txt", "content": "x", "sha256": "0" * 64}], "hops": 1}
        a.peers._drop("beta", env)
        assert await b.peers.poll_exchange() == 0
        assert b.peers.messages()[-1]["status"] == "rejected"
        assert (tmp_path / "exchange" / "beta" / "rejected" / "pmsg_files1.json").is_file()
        # an unlisted sender is refused
        stranger = {"v": 1, "id": "pmsg_x1", "from": "mallory", "to": "beta", "kind": "message", "text": "hi",
                    "files": [], "hops": 1}
        with pytest.raises(PeerError) as e:
            await b.peers.receive(stranger, "exchange")
        assert e.value.status == 403

    asyncio.run(go())


def test_signature_required_when_a_token_is_set(tmp_path, monkeypatch):
    monkeypatch.setenv("PAIR_TOKEN", "s3cret")
    a, b = pair(tmp_path, a_peer={"token_env": "PAIR_TOKEN"}, b_peer={"token_env": "PAIR_TOKEN"})

    async def go():
        t = await task_with_file(a)
        await a.peers.send("beta", "signed hello", task=t)
        assert await b.peers.poll_exchange() == 1
        forged = {"v": 1, "id": "pmsg_forged", "from": "alpha", "to": "beta", "kind": "message", "text": "evil",
                  "files": [], "hops": 1, "sig": "00"}
        a.peers._drop("beta", forged)
        good = {"v": 1, "id": "pmsg_tamper", "from": "alpha", "to": "beta", "kind": "message", "text": "ok",
                "files": [], "hops": 1}
        good["sig"] = sign(good, "s3cret")
        good["text"] = "changed after signing"
        a.peers._drop("beta", good)
        assert await b.peers.poll_exchange() == 0
        assert [m["status"] for m in b.peers.messages()[-2:]] == ["rejected", "rejected"]
        await settle(b)

    asyncio.run(go())


def test_hop_limit(tmp_path):
    a, b = pair(tmp_path, max_hops=1)

    async def go():
        env = {"v": 1, "id": "pmsg_h1", "from": "alpha", "to": "beta", "kind": "message", "text": "go on",
               "files": [], "hops": 1}
        r = await b.peers.receive(env, "exchange")
        bt = b.tasks[r["task_id"]]
        with pytest.raises(PeerError) as e:  # a task started by a peer may not start a new chain past max_hops
            await b.peers.send("alpha", "and onwards", task=bt)
        assert e.value.status == 409
        too_far = {**env, "id": "pmsg_h2", "hops": 2}
        with pytest.raises(PeerError):
            await b.peers.receive(too_far, "exchange")
        await settle(b)

    asyncio.run(go())


def test_reply_only_to_the_sender(tmp_path):
    a, b = pair(tmp_path)

    async def go():
        t = await task_with_file(a)
        with pytest.raises(PeerError) as e:
            await a.peers.send("beta", "not a reply", task=t, as_reply=True)
        assert e.value.status == 409

    asyncio.run(go())


def test_peer_tools_for_heads_only(tmp_path):
    ex = str(tmp_path / "exchange")
    bus = make(tmp_path, "alpha", {"exchange_dir": ex, "peers": {"beta": {"description": "marketing org"}}},
               departments=None)
    names = lambda a: {t["name"] for t in a.tools()[0]}  # noqa: E731
    assert {"list_peers", "send_to_peer"} <= names(bus.agents["planner"])
    head = next(a for a in bus.agents.values() if a.department != "hq" and a.is_head)
    member = next(a for a in bus.agents.values() if a.department != "hq" and not a.is_head)
    assert "send_to_peer" in names(head) and "send_to_peer" not in names(member)
    assert "beta: may send files and messages (marketing org)" in bus.agents["planner"]._system()


def test_config_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("XQO_STRICT", "1")  # xqo: plugin config errors stop startup only in strict mode
    with pytest.raises(ValueError, match="exchange_dir"):
        make(tmp_path, "alpha", {"peers": {"beta": {}}})
    assert make(tmp_path, "alpha", {"peers": {"beta": {"send": False, "token_env": "T"}}}).peers  # receive-only
    with pytest.raises(ValueError, match="plaintext token"):
        make(tmp_path, "alpha", {"peers": {"beta": {"url": "http://x:1", "token": "abc"}}})
    with pytest.raises(ValueError, match="name"):
        make(tmp_path, "bad name", {"peers": {}})
    monkeypatch.setenv("XQO_PEERING", "0")
    assert make(tmp_path, "alpha", {"peers": {"beta": {}}}).plugins.service("peering") is None


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_http_peer_api(tmp_path, monkeypatch):
    import uvicorn
    from fastapi.testclient import TestClient

    from xqo_peering import create_peer_app

    monkeypatch.setenv("AB_TOKEN", "tok-ab")
    port = _free_port()
    b = make(tmp_path, "beta", {"listen": f"127.0.0.1:{port}", "peers": {"alpha": {"token_env": "AB_TOKEN", "send": False}}})
    a = make(tmp_path, "alpha", {"peers": {"beta": {"url": f"http://127.0.0.1:{port}", "token_env": "AB_TOKEN"},
                                           "down": {"url": "http://127.0.0.1:9", "token_env": "AB_TOKEN"}}})
    app = create_peer_app(b)
    c = TestClient(app)
    env = {"v": 1, "id": "pmsg_t1", "from": "alpha", "to": "beta", "kind": "message", "text": "hi", "files": [],
           "hops": 1}
    assert c.post("/peer/v1/inbox", json=env, headers={"X-XQO-Peer": "alpha"}).status_code == 401
    assert c.post("/peer/v1/inbox", json=env, headers={"X-XQO-Peer": "alpha",
                                                        "Authorization": "Bearer nope"}).status_code == 401
    assert c.post("/peer/v1/inbox", json=env, headers={"X-XQO-Peer": "mallory",
                                                        "Authorization": "Bearer tok-ab"}).status_code == 403
    spoof = {**env, "from": "someone"}
    assert c.post("/peer/v1/inbox", json=spoof, headers={"X-XQO-Peer": "alpha",
                                                          "Authorization": "Bearer tok-ab"}).status_code == 403
    assert c.get("/peer/v1/ping", headers={"X-XQO-Peer": "alpha", "Authorization": "Bearer tok-ab"}).json()["name"] == "beta"
    assert not any(r.path.startswith("/api") for r in app.routes)  # the dashboard API is not on the peer listener

    # a real round trip over HTTP to beta's listener
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)

        async def go():
            t = await task_with_file(a)
            a.artifacts.write_bytes(t.id, "img/logo.png", PNG, t.id, "user")
            r = await a.peers.send("beta", "over http", ["docs/spec.md", "img/logo.png"], task=t)
            assert r["status"] == "delivered" and r["detail"]["task_id"]
            q = await a.peers.send("down", "nobody home", task=t)  # unreachable: queued and retried later
            assert q["status"] == "queued"
            assert a.peers.status()["queued"] == 1
            a.peers.peers.pop("down")
            await a.peers.retry_outbox()
            assert a.peers.status()["queued"] == 0
            assert a.peers.messages()[-1]["status"] == "dropped"

        asyncio.run(go())
        rec = [m for m in b.peers.messages() if m["dir"] == "in"][-1]
        assert rec["transport"] == "http" and rec["files"] == ["peers/alpha/docs/spec.md", "peers/alpha/img/logo.png"]
        assert (b.artifacts.workspace(rec["task"]) / "peers/alpha/img/logo.png").read_bytes() == PNG
        assert rec["attachments"] == ["logo.png"]
    finally:
        server.should_exit = True
        th.join(5)


def test_dashboard_api(tmp_path):
    from fastapi.testclient import TestClient

    from xqo.web import create_app

    a, b = pair(tmp_path)
    with TestClient(create_app(a)) as c:
        st = c.get("/api/peers").json()
        assert st["enabled"] and st["name"] == "alpha" and st["peers"][0]["name"] == "beta"
        rows = {p["name"]: p for p in c.get("/api/plugins").json()["items"]}
        assert rows["peering"]["status"]["name"] == "alpha"
        r = c.post("/api/peers/send", json={"peer": "beta", "text": "from the user"})
        assert r.status_code == 200 and r.json()["status"] == "delivered"
        assert c.post("/api/peers/send", json={"peer": "nobody", "text": "x"}).status_code == 404
        assert c.get("/api/peers/messages").json()[-1]["peer"] == "beta"
    assert list((tmp_path / "exchange" / "beta" / "inbox").glob("*.json"))


PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4
PDF = b"%PDF-1.4\n" + bytes(range(200, 256)) * 10 + b"\n%%EOF\n"
BLOB = bytes(range(256)) * 16  # not text, not an image


def test_binary_files_and_attachments(tmp_path):
    a, b = pair(tmp_path)

    async def go():
        t = await a.create("origin with a spec", "planner",
                           files=[{"name": "spec.pdf", "media_type": "application/pdf", "kind": "pdf", "data": PDF}])
        a.artifacts.write_bytes(t.id, "assets/logo.png", PNG, t.id, "user")
        a.artifacts.write_bytes(t.id, "data/model.bin", BLOB, t.id, "user")
        a.artifacts.write_file(t.id, "notes.md", "# notes\n", t.id, "planner")
        t.status = "done"
        with pytest.raises(Exception, match="binary file"):
            a.artifacts.read_file(t.id, "data/model.bin")
        r = await a.peers.send("beta", "Use these", ["assets/logo.png", "data/model.bin", "notes.md"], task=t,
                               attachments=["spec.pdf"], want_reply=True)
        assert r["status"] == "delivered"
        env = json.loads(next((tmp_path / "exchange" / "beta" / "inbox").glob("*.json")).read_text())
        kinds = {f["name"]: ("content" in f, f.get("attachment", False)) for f in env["files"]}
        assert kinds == {"assets/logo.png": (False, False), "data/model.bin": (False, False),
                         "notes.md": (True, False), "spec.pdf": (False, True)}
        assert await b.peers.poll_exchange() == 1
        bt = b.tasks[b.peers.messages()[-1]["task"]]
        ws = b.artifacts.workspace(bt.id)
        assert (ws / "peers/alpha/assets/logo.png").read_bytes() == PNG       # byte for byte
        assert (ws / "peers/alpha/data/model.bin").read_bytes() == BLOB
        assert (ws / "peers/alpha/notes.md").read_text() == "# notes\n"
        att = {m["name"]: m["kind"] for m in bt.attachments}
        assert att == {"spec.pdf": "pdf", "logo.png": "image"}               # the .bin stays a workspace file
        assert b.attachments.path(bt.id, "spec.pdf").read_bytes() == PDF
        assert "binary, not readable as text" in bt.prompt
        await settle(b)
        # the receiving task sends an image back as part of its reply
        b.attachments.save(bt.id, [{"name": "chart.png", "media_type": "image/png", "kind": "image", "data": PNG}])
        rr = await b.peers.send("alpha", "chart attached", task=bt, attachments=["chart.png"], as_reply=True)
        assert rr["status"] == "delivered"
        assert await a.peers.poll_exchange() == 2   # the automatic result and the chart
        assert any(m["name"] == "chart.png" for m in t.attachments)
        assert a.attachments.path(t.id, "chart.png").read_bytes() == PNG
        await settle(a)

    asyncio.run(go())


def test_corrupt_binary_is_rejected(tmp_path):
    a, b = pair(tmp_path)

    async def go():
        import base64, hashlib
        good = {"name": "x.bin", "bytes": len(BLOB), "sha256": hashlib.sha256(BLOB).hexdigest(),
                "data": base64.b64encode(BLOB).decode(), "media_type": "application/octet-stream"}
        base = {"v": 1, "from": "alpha", "to": "beta", "kind": "message", "text": "", "hops": 1}
        for i, f in enumerate([{**good, "data": "not base64!!"}, {**good, "sha256": "0" * 64},
                               {**good, "content": "both"}, {**good, "name": "../escape.bin"}]):
            with pytest.raises(PeerError):
                await b.peers.receive({**base, "id": f"pmsg_bad{i}", "files": [f]}, "exchange")
        not_an_image = {**good, "attachment": True}  # an attachment must be an image, PDF or text file
        with pytest.raises(PeerError, match="attachment refused"):
            await b.peers.receive({**base, "id": "pmsg_bad9", "files": [not_an_image]}, "exchange")
        assert not b.tasks

    asyncio.run(go())


def test_binary_workspace_files_rollback_and_fork(tmp_path):
    from xqo.artifacts import ArtifactStore
    s = ArtifactStore(tmp_path / "art")
    v1 = s.write_bytes("t1", "img/a.png", PNG, "t1", "peer:x")
    s.write_bytes("t1", "img/a.png", PNG[::-1], "t1", "peer:x")
    s.write_file("t1", "notes.md", "hi\n", "t1", "planner")
    rb = s.rollback("t1", "img/a.png", v1["path"])
    assert rb["action"] == "rollback" and (s.workspace("t1") / "img/a.png").read_bytes() == PNG
    state = s.state_from(s.list("t1"))
    assert state == {"img/a.png": PNG, "notes.md": "hi\n"}
    s.restore("t2", state, "user", "fork:t1", "fork")
    assert (s.workspace("t2") / "img/a.png").read_bytes() == PNG
    assert (s.workspace("t2") / "notes.md").read_text() == "hi\n"


def test_limits_from_config_and_env(tmp_path, monkeypatch):
    from xqo_peering.hub import Limits
    assert Limits().max_file_bytes == 20_000_000 and Limits().queue_seconds == 24 * 3600
    lim = Limits({"max_file_mb": 50, "max_total_mb": 100, "retry_seconds": 5, "queue_hours": 2, "max_files": 40})
    assert (lim.max_file_bytes, lim.max_total_bytes, lim.retry_seconds, lim.queue_seconds, lim.max_files) == \
        (50_000_000, 100_000_000, 5, 7200, 40)
    assert lim.max_body_bytes > 100_000_000 * 4 // 3
    monkeypatch.setenv("XQO_PEER_MAX_FILE_MB", "0.5")
    assert Limits({"max_file_mb": 50}).max_file_bytes == 500_000       # the environment wins
    with pytest.raises(ValueError, match="unknown"):
        Limits({"max_size": 1})
    with pytest.raises(ValueError, match="positive"):
        Limits({"max_files": 0})


def test_limits_are_enforced_on_both_sides(tmp_path):
    ex = str(tmp_path / "exchange")
    a = make(tmp_path, "alpha", {"exchange_dir": ex, "peers": {"beta": {}}, "limits": {"max_file_mb": 1}})
    b = make(tmp_path, "beta", {"exchange_dir": ex, "peers": {"alpha": {}}, "limits": {"max_file_mb": 0.001}})

    async def go():
        t = await task_with_file(a)
        a.artifacts.write_bytes(t.id, "big.bin", BLOB * 300, t.id, "user")   # ~1.2 MB
        with pytest.raises(PeerError, match="too large"):
            await a.peers.send("beta", "", ["big.bin"], task=t)
        await a.peers.send("beta", "", ["docs/spec.md"], task=t)  # small: goes out
        a.artifacts.write_bytes(t.id, "mid.bin", BLOB, t.id, "user")         # 4 KB: fine for alpha, not for beta
        await a.peers.send("beta", "", ["mid.bin"], task=t)
        assert await b.peers.poll_exchange() == 1
        rejected = [m for m in b.peers.messages() if m["status"] == "rejected"]
        assert len(rejected) == 1 and "peering.limits" in rejected[0]["detail"]
        await settle(b)

    asyncio.run(go())


def test_large_text_files_travel_as_bytes(tmp_path):
    a, b = pair(tmp_path)

    async def go():
        t = await task_with_file(a)
        big = ("x" * 99 + "\n") * 60_000                                    # 6 MB of text: over the 5 MB workspace limit
        a.artifacts.write_bytes(t.id, "big.log", big.encode(), t.id, "user")
        await a.peers.send("beta", "log", ["big.log"], task=t)
        assert await b.peers.poll_exchange() == 1
        bt = b.tasks[b.peers.messages()[-1]["task"]]
        assert (b.artifacts.workspace(bt.id) / "peers/alpha/big.log").read_text() == big
        await settle(b)

    asyncio.run(go())


def test_http_listener_refuses_bodies_over_the_limit(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from xqo_peering import create_peer_app
    monkeypatch.setenv("AB_TOKEN", "tok")
    b = make(tmp_path, "beta", {"listen": "127.0.0.1:1", "limits": {"max_total_mb": 0.001, "max_text_chars": 10},
                                "peers": {"alpha": {"token_env": "AB_TOKEN", "send": False}}})
    c = TestClient(create_peer_app(b))
    r = c.post("/peer/v1/inbox", content=b"x" * 1_100_000,
               headers={"X-XQO-Peer": "alpha", "Authorization": "Bearer tok", "Content-Type": "application/json"})
    assert r.status_code == 413
