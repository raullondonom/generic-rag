"""Against a real Qdrant (TEST_QDRANT_URL): collection with BM25, hybrid search and text filters."""

import os

import pytest

from rag import sparse
from rag.config import Settings
from rag.vectorstore import VectorStore

URL = os.environ.get("TEST_QDRANT_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_QDRANT_URL not set")


def test_dense_sparse_and_text_search_on_a_real_collection():
    settings = Settings(_env_file=None, qdrant_url=URL, collection_prefix="pytest", language="en")
    store = VectorStore(settings)
    if store.exists():
        store.client.delete_collection(store.collection)
    try:
        chunks = ["R.12 Reimbursement form: travel costs are refunded within 30 days",
                  "Opening hours of the offices in every region"]
        vectors = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
        payloads = [{"source": "folder", "title": "Policy", "status": ""}, {"source": "folder", "title": "Guide"}]
        store.upsert("folder:a", chunks, vectors, payloads,
                     sparse=[sparse.document_vector(c, "en") for c in chunks])
        store.ensure_text_index()
        dense = store.search([0.0, 1.0, 0.0], 2)
        assert dense[0].payload["title"] == "Guide"
        lexical = store.search_sparse(sparse.query_vector("how do I get travel costs reimbursed", "en"), 2)
        assert [h.payload["title"] for h in lexical] == ["Policy"]
        assert store.search([0.0, 1.0, 0.0], 2, text="reimbursement form")[0].payload["title"] == "Policy"
    finally:
        store.client.delete_collection(store.collection)
