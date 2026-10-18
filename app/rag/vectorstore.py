"""Qdrant access: one point per chunk, in a collection of its own for each embeddings model."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

from .config import Settings

log = logging.getLogger(__name__)


@dataclass
class Hit:
    score: float
    text: str
    payload: dict


class VectorStore:
    def __init__(self, settings: Settings, client: QdrantClient | None = None):
        self.prefix = settings.collection_prefix
        self.collection = settings.collection_name
        self.client = client or QdrantClient(url=settings.qdrant_url, timeout=60)
        self._ready = False

    def ensure_collection(self, dim: int) -> None:
        if self._ready:
            return
        if not self.client.collection_exists(self.collection):
            self.client.create_collection(
                self.collection,
                vectors_config=qm.VectorParams(size=dim, distance=qm.Distance.COSINE),
            )
            for field in ("doc_id", "source", "status"):
                self.client.create_payload_index(self.collection, field, qm.PayloadSchemaType.KEYWORD)
        self._ready = True

    def exists(self) -> bool:
        return self.client.collection_exists(self.collection)

    def drop_other_collections(self) -> list[str]:
        """Deletes the collections of previous models (same prefix, different model)."""
        dropped = []
        for c in self.client.get_collections().collections:
            name = c.name
            if name != self.collection and (name == self.prefix or name.startswith(self.prefix + "__")):
                self.client.delete_collection(name)
                dropped.append(name)
        if dropped:
            log.info("Deleted collections of previous models: %s", ", ".join(dropped))
        return dropped

    def upsert(self, doc_id: str, chunks: list[str], vectors: list[list[float]], payloads: list[dict]) -> None:
        self.ensure_collection(len(vectors[0]))
        points = [
            qm.PointStruct(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{doc_id}#{i}")),
                vector=vec,
                payload={**meta, "doc_id": doc_id, "chunk_index": i, "text": text},
            )
            for i, (text, vec, meta) in enumerate(zip(chunks, vectors, payloads))
        ]
        for i in range(0, len(points), 128):
            self.client.upsert(self.collection, points=points[i:i + 128], wait=True)

    def delete_doc(self, doc_id: str) -> None:
        if not self.exists():
            return
        self.client.delete(
            self.collection,
            points_selector=qm.FilterSelector(
                filter=qm.Filter(must=[qm.FieldCondition(key="doc_id", match=qm.MatchValue(value=doc_id))])
            ),
            wait=True,
        )

    def search(self, vector: list[float], limit: int, exclude_statuses: list[str] | None = None,
               where: dict[str, str] | None = None) -> list[Hit]:
        """Nearest chunks. Chunks whose registry `status` is in `exclude_statuses` (e.g. repealed) are skipped;
        `where` keeps only the chunks whose payload has those values ({field: value})."""
        if not self.exists():
            return []
        must_not = [qm.FieldCondition(key="status", match=qm.MatchAny(any=list(exclude_statuses)))] \
            if exclude_statuses else None
        must = [qm.FieldCondition(key=k, match=qm.MatchValue(value=v)) for k, v in (where or {}).items()] or None
        query_filter = qm.Filter(must=must, must_not=must_not) if must or must_not else None
        result = self.client.query_points(self.collection, query=vector, limit=limit, with_payload=True,
                                          query_filter=query_filter)
        return [Hit(score=p.score, text=p.payload.get("text", ""), payload=p.payload) for p in result.points]

    def ensure_keyword_index(self, field: str) -> None:
        """Payload index needed to list a field's values and filter by it (idempotent)."""
        if self.exists():
            self.client.create_payload_index(self.collection, field, qm.PayloadSchemaType.KEYWORD, wait=True)

    def values(self, field: str, limit: int = 1000) -> list[str]:
        """Distinct values of a payload field in the index (to fill a selector), alphabetical."""
        if not self.exists():
            return []
        self.ensure_keyword_index(field)
        result = self.client.facet(self.collection, key=field, limit=limit, exact=True)
        return sorted((str(h.value) for h in result.hits if str(h.value).strip()), key=str.casefold)

    def count(self) -> int:
        if not self.exists():
            return 0
        return self.client.count(self.collection, exact=False).count
