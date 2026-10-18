import httpx
import pytest

from rag.config import Settings
from rag.vectorstore import VectorStore


class FakeClient:
    def __init__(self):
        self.created = None

    def collection_exists(self, name):
        return False

    def create_collection(self, name, **kw):
        self.created = kw

    def create_payload_index(self, *a, **kw):
        pass


def store(monkeypatch, peers, **settings):
    s = Settings(qdrant_url="http://q:6333", **settings)

    def fake_get(url, timeout):
        assert url == "http://q:6333/cluster"
        return httpx.Response(200, json={"result": {"status": "enabled", "peers": {str(i): {} for i in range(peers)}}},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr("rag.vectorstore.httpx.get", fake_get)
    vs = VectorStore(s, client=FakeClient())
    vs.ensure_text_index = lambda: None
    return vs


def test_collection_is_created_with_the_configured_copies(monkeypatch):
    vs = store(monkeypatch, 3, qdrant_replication_factor=3, qdrant_write_consistency=2)
    vs.ensure_collection(4)
    assert vs.client.created["replication_factor"] == 3
    assert vs.client.created["write_consistency_factor"] == 2
    assert vs.client.created["shard_number"] == 1


def test_creation_waits_until_every_node_is_up(monkeypatch):
    vs = store(monkeypatch, 2, qdrant_replication_factor=3)
    with pytest.raises(RuntimeError, match="3 are needed"):
        vs.ensure_collection(4)
    assert vs.client.created is None


def test_single_node_defaults_do_not_ask_the_cluster(monkeypatch):
    vs = store(monkeypatch, 0)  # a /cluster call would fail the assertion
    monkeypatch.setattr("rag.vectorstore.httpx.get", lambda *a, **k: pytest.fail("no cluster call expected"))
    vs.ensure_collection(4)
    assert vs.client.created["replication_factor"] == 1


def test_write_consistency_never_exceeds_the_copies(monkeypatch):
    vs = store(monkeypatch, 1, qdrant_replication_factor=1, qdrant_write_consistency=3)
    vs.ensure_collection(4)
    assert vs.client.created["write_consistency_factor"] == 1
