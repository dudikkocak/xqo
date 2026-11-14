"""Task attachments: validation, storage, content blocks, forwarding, API."""
import asyncio
import base64
import struct
import zlib

import pytest
from fastapi.testclient import TestClient

from xqo.attachments import AttachmentStore, classify, safe_name, validate
from xqo.bus import HandoverError, MessageBus
from xqo.config import build
from xqo.web import create_app


def png() -> bytes:
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b""))


PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"


def test_classify():
    assert classify("a.png", "image/png", png()) == ("image", "image/png")
    assert classify("shot", None, png()) == ("image", "image/png")  # sniffed from the bytes
    assert classify("photo.jpg", "image/jpg", b"\xff\xd8\xff\xe0rest") == ("image", "image/jpeg")
    assert classify("doc.pdf", None, PDF) == ("pdf", "application/pdf")
    assert classify("x.bin", "application/octet-stream", PDF) == ("pdf", "application/pdf")
    assert classify("notes.md", None, "héllo".encode()) == ("text", "text/plain")
    assert classify("data.json", "application/json", b"{}") == ("text", "text/plain")
    with pytest.raises(ValueError, match="unsupported image"):
        classify("fake.png", "image/png", b"not an image")
    with pytest.raises(ValueError, match="unsupported file"):
        classify("a.zip", "application/zip", b"PK\x03\x04\x00\x00binary")


def test_validate_limits():
    with pytest.raises(ValueError, match="at most"):
        validate([("a.txt", None, b"x")] * 21)
    with pytest.raises(ValueError, match="limited to 5 MB"):
        validate([("big.png", "image/png", png() + b"\x00" * 5_000_001)])
    with pytest.raises(ValueError, match="empty"):
        validate([("e.txt", "text/plain", b"")])
    v = validate([("a.txt", None, b"hi"), ("b.png", None, png())])
    assert [x["kind"] for x in v] == ["text", "image"]


def test_store_names_and_blocks(tmp_path):
    s = AttachmentStore(tmp_path)
    assert safe_name("../../etc/passwd") == "passwd" and safe_name("C:\\x\\my file.txt") == "my_file.txt"
    metas = s.save("r", validate([("a.txt", None, b"one"), ("a.txt", None, b"two"), ("p.png", None, png()),
                                  ("d.pdf", None, PDF)]))
    assert [m["name"] for m in metas] == ["a.txt", "a-2.txt", "p.png", "d.pdf"]
    blocks = s.blocks("r", metas)
    assert blocks[0] == {"type": "document", "title": "a.txt",
                         "source": {"type": "text", "media_type": "text/plain", "data": "one"}}
    assert blocks[2]["type"] == "image" and base64.b64decode(blocks[2]["source"]["data"]) == png()
    assert blocks[3]["type"] == "document" and blocks[3]["source"]["media_type"] == "application/pdf"
    assert s.path("r", "../r/attachments/a.txt").name == "a.txt"  # traversal stripped
    assert s.path("r", "missing.txt") is None


def mock_bus(tmp_path):
    return build("agents.json", tmp_path / "artifacts", str(tmp_path / "logs" / "events.jsonl"), mock=True)


def test_attachments_reach_the_model_and_persist(tmp_path):
    b = mock_bus(tmp_path)
    first_msgs = {}
    llm = b.agents["planner"].llm
    orig = llm.create

    async def spy(**kw):
        first_msgs.setdefault(kw["agent"], kw["messages"][0]["content"])
        return await orig(**kw)
    llm.create = spy
    files = validate([("spec.md", None, b"# Spec\nfib(n)"), ("ui.png", "image/png", png())])
    t = asyncio.run(b.submit("Build what the spec says", files=files))
    assert t.status == "done"
    content = first_msgs["planner"]
    assert isinstance(content, list) and [c["type"] for c in content] == ["document", "image", "text"]
    assert "spec.md (text" in content[-1]["text"] and "Build what the spec says" in content[-1]["text"]
    assert isinstance(first_msgs["coder"], str)  # not forwarded unless an agent asks
    assert [a["name"] for a in t.attachments] == ["spec.md", "ui.png"]
    ev = b.task_events(t.id)[0]
    assert ev["meta"]["attachments"][1] == {"name": "ui.png", "media_type": "image/png", "kind": "image",
                                            "bytes": len(png())}
    assert MessageBus(b.log_path).tasks[t.id].attachments == t.attachments  # survives restart

    # forwarding resolves names against the root task
    child = b._child(t, "coder", "look at the mockup", "Mockup", ["ui.png"])
    assert [a["name"] for a in child.attachments] == ["ui.png"]
    with pytest.raises(HandoverError, match="unknown attachment"):
        b._child(t, "coder", "x", "x", ["nope.png"])

    # forks keep the attachments
    f = asyncio.run(b.fork(t.id, ev["id"]))
    assert f.attachments == t.attachments and b.attachments.path(f.id, "ui.png").read_bytes() == png()


def test_api_attachments(tmp_path):
    b = mock_bus(tmp_path)
    enc = lambda d: base64.b64encode(d).decode()
    with TestClient(create_app(b)) as c:
        assert c.post("/api/tasks", json={"prompt": " "}).status_code == 422
        r = c.post("/api/tasks", json={"prompt": "", "attachments": [{"name": "x.png", "data": "@@@"}]})
        assert r.status_code == 400 and "base64" in r.json()["detail"]
        r = c.post("/api/tasks", json={"prompt": "x", "attachments": [
            {"name": "a.zip", "media_type": "application/zip", "data": enc(b"PK\x03\x04\x00binary")}]})
        assert r.status_code == 400 and "unsupported" in r.json()["detail"]
        r = c.post("/api/tasks", json={"prompt": "", "attachments": [
            {"name": "image.png", "media_type": "image/png", "data": "data:image/png;base64," + enc(png())},
            {"name": "pasted.txt", "media_type": "text/plain", "data": enc(b"<script>alert(1)</script>")}]})
        assert r.status_code == 200 and [a["kind"] for a in r.json()["attachments"]] == ["image", "text"]
        for _ in range(200):
            t = next((t for t in b.tasks.values() if t.attachments), None)
            if t and t.status == "done":
                break
            asyncio.run(asyncio.sleep(0.02))
        assert t.title.startswith("Attached files")
        img = c.get(f"/api/attachments/{t.id}/image.png")
        assert img.status_code == 200 and img.headers["content-type"] == "image/png" and img.content == png()
        txt = c.get(f"/api/attachments/{t.id}/pasted.txt")
        assert txt.headers["content-type"].startswith("text/plain") and "sandbox" in txt.headers["content-security-policy"]
        assert c.get(f"/api/attachments/{t.id}/nope.txt").status_code == 404
