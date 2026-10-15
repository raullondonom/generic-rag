"""Embeddings locales con Ollama (los documentos nunca salen de la máquina)."""

from __future__ import annotations

import httpx

from .config import Settings


class Embedder:
    def __init__(self, settings: Settings, batch_size: int = 16):
        self.model = settings.embed_model
        self.batch_size = batch_size
        self._client = httpx.Client(base_url=settings.ollama_url, timeout=httpx.Timeout(300.0, connect=10.0))

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i:i + self.batch_size]
            resp = self._client.post("/api/embed", json={"model": self.model, "input": batch, "truncate": True})
            resp.raise_for_status()
            vectors.extend(resp.json()["embeddings"])
        return vectors

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]
