"""Local embeddings with text-embeddings-inference (TEI): documents never leave the machine.

"Instruct" models (Qwen3-Embedding, e5-instruct) work better when the query is prefixed with an
instruction; documents are embedded as-is (classic e5 uses "query: " / "passage: ").
"""

from __future__ import annotations

import random
import time

import httpx

from .config import Settings

RETRIES = 4

_QUERY_PREFIXES = {
    "qwen/qwen3-embedding": "Instruct: {instruction}\nQuery:",
    "intfloat/multilingual-e5-large-instruct": "Instruct: {instruction}\nQuery: ",
    "intfloat/multilingual-e5-": "query: ",
    "snowflake/snowflake-arctic-embed": "query: ",
}
_DOCUMENT_PREFIXES = {"intfloat/multilingual-e5-": "passage: "}


def _prefix(table: dict[str, str], model: str) -> str:
    model = model.lower()
    for key in sorted(table, key=len, reverse=True):
        if model.startswith(key):
            return table[key]
    return ""


def query_prefix(model: str, instruction: str) -> str:
    return _prefix(_QUERY_PREFIXES, model).replace("{instruction}", instruction)


def document_prefix(model: str) -> str:
    if "instruct" in model.lower():
        return ""
    return _prefix(_DOCUMENT_PREFIXES, model)


class Embedder:
    def __init__(self, settings: Settings):
        self.model = settings.embedding_model
        self.batch_size = settings.embedding_batch
        self._query_prefix = query_prefix(self.model, settings.embedding_instruction)
        self._document_prefix = document_prefix(self.model)
        self._client = httpx.Client(base_url=settings.embedding_url, timeout=httpx.Timeout(300.0, connect=10.0))

    def info(self) -> dict:
        resp = self._client.get("/info", timeout=5)
        resp.raise_for_status()
        return resp.json()

    def _post(self, batch: list[str]) -> list[list[float]]:
        """A node that fails or is overloaded (429, 5xx) is retried: behind a balancer the retry usually lands on
        another one. Other errors (4xx) are not transient."""
        for attempt in range(RETRIES):
            try:
                resp = self._client.post("/embed", json={"inputs": batch, "truncate": True})
                if resp.status_code != 429 and resp.status_code < 500:
                    resp.raise_for_status()
                    return resp.json()
                error: Exception = httpx.HTTPStatusError(str(resp.status_code), request=resp.request, response=resp)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                error = e
            if attempt == RETRIES - 1:
                raise error
            time.sleep(min(0.5 * 2 ** attempt, 4) * (0.5 + random.random()))
        raise AssertionError("unreachable")

    def _embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            vectors.extend(self._post(texts[i:i + self.batch_size]))
        return vectors

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed([self._document_prefix + t for t in texts])

    def embed_query(self, text: str) -> list[float]:
        return self._embed([self._query_prefix + text])[0]

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        return self._embed([self._query_prefix + t for t in texts])
