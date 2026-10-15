"""Acceso a Qdrant: una colección con un punto por fragmento (chunk)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

from .config import Settings


@dataclass
class Hit:
    score: float
    text: str
    payload: dict


class VectorStore:
    def __init__(self, settings: Settings):
        self.collection = settings.collection_name
        self.client = QdrantClient(url=settings.qdrant_url, timeout=60)
        self._ready = False

    def ensure_collection(self, dim: int) -> None:
        if self._ready:
            return
        if not self.client.collection_exists(self.collection):
            self.client.create_collection(
                self.collection,
                vectors_config=qm.VectorParams(size=dim, distance=qm.Distance.COSINE),
            )
            for field in ("doc_id", "source"):
                self.client.create_payload_index(self.collection, field, qm.PayloadSchemaType.KEYWORD)
        self._ready = True

    def exists(self) -> bool:
        return self.client.collection_exists(self.collection)

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

    def delete_source(self, source: str) -> None:
        if not self.exists():
            return
        self.client.delete(
            self.collection,
            points_selector=qm.FilterSelector(
                filter=qm.Filter(must=[qm.FieldCondition(key="source", match=qm.MatchValue(value=source))])
            ),
            wait=True,
        )

    def search(self, vector: list[float], limit: int) -> list[Hit]:
        if not self.exists():
            return []
        result = self.client.query_points(self.collection, query=vector, limit=limit, with_payload=True)
        return [Hit(score=p.score, text=p.payload.get("text", ""), payload=p.payload) for p in result.points]

    def count(self) -> int:
        if not self.exists():
            return 0
        return self.client.count(self.collection, exact=False).count
