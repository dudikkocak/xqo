"""xqo-peering is found through its entry point, names the office's HQ, and keeps its state folder in backups."""
from fastapi.testclient import TestClient

from xqo import backup
from xqo.web import create_app

from test_peers import make


def test_plugin_is_active_and_the_office_shows_the_peer_hq(tmp_path):
    bus = make(tmp_path, "alpha", {"exchange_dir": str(tmp_path / "ex"), "peers": {"beta": {}}},
               office={"hqs": [{"name": "beta", "url": "http://127.0.0.1:9"}]})
    st = bus.plugins.states["peering"]
    assert st.state == "active" and st.source == "xqo_peering:PeeringPlugin"
    assert ".peers" in backup.state_dirs()
    with TestClient(create_app(bus)) as c:
        office = c.get("/api/office").json()
        assert office["name"] == "alpha"  # the office takes its own HQ name from peering
        assert [h["name"] for h in office["hqs"]] == ["beta"]  # the remote HQ in the planners world
        assert c.get("/plugins/peering/ui.js").status_code == 200


def test_without_peering_the_office_is_hq(tmp_path, monkeypatch):
    monkeypatch.setenv("XQO_PEERING", "0")
    bus = make(tmp_path, "alpha", {"peers": {"beta": {}}})
    with TestClient(create_app(bus)) as c:
        assert c.get("/api/office").json()["name"] == "hq"
