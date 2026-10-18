"""Qdrant access: one point per chunk, in a collection of its own for each embeddings model."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

from .config import Settings
from .i18n import normalize

log = logging.getLogger(__name__)

SPARSE = "bm25"  # name of the sparse vector of every point


def _filter(exclude_statuses: list[str] | None, where: dict[str, str] | None, text: str | None) -> qm.Filter | None:
    must_not = [qm.FieldCondition(key="status", match=qm.MatchAny(any=list(exclude_statuses)))] \
        if exclude_statuses else None
    must = [qm.FieldCondition(key=k, match=qm.MatchValue(value=v)) for k, v in (where or {}).items()]
    if text:
        must.append(text_condition(text))
    return qm.Filter(must=must or None, must_not=must_not) if must or must_not else None


# LANGUAGE -> stemmer and stopwords of the full-text index
TEXT_LANGUAGES = {"en": "english", "es": "spanish"}


TEXT_FIELDS = ("text", "search_note")  # the chunk and its search note (contextual indexing)


def text_condition(text: str) -> qm.Filter:
    """A keyword of several words must appear as that phrase; a single word, in any form (stemmed).
    It may appear in the chunk or in its search note."""
    text = text.strip()
    match = qm.MatchPhrase(phrase=text) if len(text.split()) > 1 else qm.MatchText(text=text)
    return qm.Filter(should=[qm.FieldCondition(key=field, match=match) for field in TEXT_FIELDS])


@dataclass
class Hit:
    score: float
    text: str
    payload: dict


class VectorStore:
    def __init__(self, settings: Settings, client: QdrantClient | None = None):
        self.prefix = settings.collection_prefix
        self.collection = settings.collection_name
        self.language = normalize(settings.language)
        self.sparse = settings.sparse_search
        self.client = client or QdrantClient(url=settings.qdrant_url, timeout=60)
        self._ready = False

    def ensure_collection(self, dim: int) -> None:
        if self._ready:
            return
        if not self.client.collection_exists(self.collection):
            self.client.create_collection(
                self.collection,
                vectors_config=qm.VectorParams(size=dim, distance=qm.Distance.COSINE),
                # BM25: the points carry the term weights, Qdrant applies the IDF of the collection
                sparse_vectors_config={SPARSE: qm.SparseVectorParams(modifier=qm.Modifier.IDF)}
                if self.sparse else None,
            )
            for field in ("doc_id", "source", "status"):
                self.client.create_payload_index(self.collection, field, qm.PayloadSchemaType.KEYWORD)
            self.ensure_text_index()
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

    def upsert(self, doc_id: str, chunks: list[str], vectors: list[list[float]], payloads: list[dict],
               sparse: list[tuple[list[int], list[float]]] | None = None) -> None:
        """`sparse`: the BM25 vector (term ids, weights) of every chunk, when the collection has them."""
        self.ensure_collection(len(vectors[0]))

        def vector(i: int, vec: list[float]):
            if not (self.sparse and sparse):
                return vec
            indices, values = sparse[i]
            return {"": vec, SPARSE: qm.SparseVector(indices=indices, values=values)}

        points = [
            qm.PointStruct(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{doc_id}#{i}")),
                vector=vector(i, vec),
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
               where: dict[str, str] | None = None, text: str | None = None) -> list[Hit]:
        """Nearest chunks. Chunks whose registry `status` is in `exclude_statuses` (e.g. repealed) are skipped;
        `where` keeps only the chunks whose payload has those values ({field: value}) and `text` only those
        containing all its words (full-text index)."""
        if not self.exists():
            return []
        result = self.client.query_points(self.collection, query=vector, limit=limit, with_payload=True,
                                          query_filter=_filter(exclude_statuses, where, text))
        return [Hit(score=p.score, text=p.payload.get("text", ""), payload=p.payload) for p in result.points]

    def search_sparse(self, query: tuple[list[int], list[float]], limit: int,
                      exclude_statuses: list[str] | None = None, where: dict[str, str] | None = None) -> list[Hit]:
        """BM25 search with a query's term vector (scores are BM25, not comparable with cosine)."""
        indices, values = query
        if not (self.sparse and indices and self.exists()):
            return []
        result = self.client.query_points(self.collection, query=qm.SparseVector(indices=indices, values=values),
                                          using=SPARSE, limit=limit, with_payload=True,
                                          query_filter=_filter(exclude_statuses, where, None))
        return [Hit(score=p.score, text=p.payload.get("text", ""), payload=p.payload) for p in result.points]

    def ensure_text_index(self) -> None:
        """Full-text index of the chunks for keyword filters: lowercase, without accents, stemmed and
        without stopwords in the collection's language. Built on the stored chunks (no re-embedding)."""
        if not self.exists():
            return
        lang = TEXT_LANGUAGES.get(self.language, "english")
        params = qm.TextIndexParams(
            type=qm.TextIndexType.TEXT, tokenizer=qm.TokenizerType.WORD, lowercase=True, ascii_folding=True,
            min_token_len=2, max_token_len=40, stopwords=qm.Language(lang), phrase_matching=True,
            stemmer=qm.SnowballParams(type=qm.Snowball.SNOWBALL, language=qm.SnowballLanguage(lang)),
        )
        schema = self.client.get_collection(self.collection).payload_schema
        for field in TEXT_FIELDS:
            current = schema.get(field)
            if current is not None and current.params is not None:
                if current.params.model_dump(exclude_none=True) == params.model_dump(exclude_none=True):
                    continue
                self.client.delete_payload_index(self.collection, field, wait=True)  # settings changed
            self.client.create_payload_index(self.collection, field, field_schema=params, wait=False)

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
        # The facet can keep values whose chunks were all replaced (count 0) until Qdrant optimizes
        return sorted((str(h.value) for h in result.hits if h.count > 0 and str(h.value).strip()), key=str.casefold)

    def count_text(self, text: str) -> int:
        """Chunks that contain all the words of `text` (full-text index)."""
        if not self.exists():
            return 0
        flt = qm.Filter(must=[text_condition(text)])
        return self.client.count(self.collection, count_filter=flt, exact=True).count

    def count(self) -> int:
        if not self.exists():
            return 0
        return self.client.count(self.collection, exact=False).count
