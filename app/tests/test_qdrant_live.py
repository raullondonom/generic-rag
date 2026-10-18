"""Against a real Qdrant (TEST_QDRANT_URL): collection with BM25, hybrid search and text filters."""

import os

import pytest

from rag import sparse
from rag.config import Settings
from rag.vectorstore import VectorStore

URL = os.environ.get("TEST_QDRANT_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_QDRANT_URL not set")


def test_dense_sparse_and_text_search_on_a_real_collection():
    settings = Settings(_env_file=None, qdrant_url=URL, collection_prefix="pytest", language="es")
    store = VectorStore(settings)
    if store.exists():
        store.client.delete_collection(store.collection)
    try:
        chunks = ["C.11 No portar el equipo de prevención y seguridad: extintor, botiquín",
                  "Pico y placa para vehículos particulares en la ciudad"]
        vectors = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
        payloads = [{"source": "folder", "title": "Ley", "status": ""}, {"source": "folder", "title": "Decreto"}]
        store.upsert("folder:a", chunks, vectors, payloads,
                     sparse=[sparse.document_vector(c, "es") for c in chunks])
        store.ensure_text_index()
        dense = store.search([0.0, 1.0, 0.0], 2)
        assert dense[0].payload["title"] == "Decreto"
        lexical = store.search_sparse(sparse.query_vector("multa por no llevar extintor", "es"), 2)
        assert [h.payload["title"] for h in lexical] == ["Ley"]
        assert store.search([0.0, 1.0, 0.0], 2, text="equipo de prevención")[0].payload["title"] == "Ley"
    finally:
        store.client.delete_collection(store.collection)
