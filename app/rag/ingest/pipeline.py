"""Indexación de un documento: secciones -> fragmentos -> embeddings -> Qdrant + estado."""

from __future__ import annotations

import logging

from ..config import Settings
from ..embeddings import Embedder
from ..vectorstore import VectorStore
from .chunker import chunk_text
from .loaders import Section
from .state import StateStore

log = logging.getLogger(__name__)


class Indexer:
    def __init__(self, settings: Settings, embedder: Embedder, store: VectorStore, state: StateStore):
        self.settings = settings
        self.embedder = embedder
        self.store = store
        self.state = state

    def is_current(self, doc_id: str, version: str) -> bool:
        """True si el documento ya está procesado con esa versión (y no quedó en error)."""
        doc = self.state.get(doc_id)
        return bool(doc and doc["version"] == version and doc["status"] != "error")

    def index(self, doc_id: str, *, source: str, title: str, location: str, url: str, version: str,
              sections: list[Section], container: str | None = None) -> int:
        chunks: list[str] = []
        payloads: list[dict] = []
        for section in sections:
            header = title + (f" ({section.location})" if section.location else "")
            for piece in chunk_text(section.text, self.settings.chunk_size, self.settings.chunk_overlap):
                # El título va dentro del fragmento: mejora la búsqueda y da contexto a la IA
                chunks.append(f"{header}\n\n{piece}")
                payloads.append({
                    "source": source,
                    "title": title,
                    "location": location,
                    "url": url,
                    "section": section.location,
                    "container": container or "",
                })

        self.store.delete_doc(doc_id)
        if not chunks:
            self.state.upsert(doc_id, source=source, title=title, location=location, version=version,
                              status="empty", container=container, error="sin texto extraíble")
            return 0

        vectors = self.embedder.embed(chunks)
        self.store.upsert(doc_id, chunks, vectors, payloads)
        self.state.upsert(doc_id, source=source, title=title, location=location, version=version,
                          status="indexed", chunks=len(chunks), container=container)
        log.info("Indexado %s (%d fragmentos)", location, len(chunks))
        return len(chunks)

    def mark(self, doc_id: str, *, source: str, title: str, location: str, version: str, status: str,
             error: str, container: str | None = None) -> None:
        """Registra un documento que no se pudo indexar (no soportado o error)."""
        self.store.delete_doc(doc_id)
        self.state.upsert(doc_id, source=source, title=title, location=location, version=version,
                          status=status, error=error[:500], container=container)

    def remove(self, doc_id: str) -> None:
        self.store.delete_doc(doc_id)
        self.state.delete(doc_id)
        log.info("Eliminado del índice: %s", doc_id)
