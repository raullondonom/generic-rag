"""Indexing one document: sections -> redaction -> chunks -> embeddings -> Qdrant + state."""

from __future__ import annotations

import logging

from ..config import Settings
from ..embeddings import Embedder
from ..i18n import t
from ..redaction import Redactor
from ..vectorstore import VectorStore
from .chunker import chunk_text
from .loaders import Section
from .state import StateStore

log = logging.getLogger(__name__)


class Indexer:
    def __init__(self, settings: Settings, embedder: Embedder, store: VectorStore, state: StateStore,
                 redactor: Redactor | None = None):
        self.settings = settings
        self.embedder = embedder
        self.store = store
        self.state = state
        self.redactor = redactor or Redactor.from_settings(settings)

    def is_current(self, doc_id: str, version: str) -> bool:
        """True if the document was already processed with that version (and did not end in error)."""
        doc = self.state.get(doc_id)
        return bool(doc and doc["version"] == version and doc["status"] != "error")

    def index(self, doc_id: str, *, source: str, title: str, location: str, url: str, version: str,
              sections: list[Section], container: str | None = None) -> int:
        chunks: list[str] = []
        payloads: list[dict] = []
        redactions = 0
        for section in sections:
            # Redact before chunking: what reaches Qdrant (and later the AI) is already clean
            result = self.redactor(section.text)
            redactions += result.total
            header = title + (f" ({section.location})" if section.location else "")
            for piece in chunk_text(result.text, self.settings.chunk_size, self.settings.chunk_overlap):
                # The title goes inside the chunk: better search and context for the AI
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
                              status="empty", container=container, error=t(self.settings.language, "no_text"))
            return 0

        vectors = self.embedder.embed_documents(chunks)
        self.store.upsert(doc_id, chunks, vectors, payloads)
        self.state.upsert(doc_id, source=source, title=title, location=location, version=version,
                          status="indexed", chunks=len(chunks), redactions=redactions, container=container)
        log.info("Indexed %s (%d chunks%s)", location, len(chunks),
                 f", {redactions} values redacted" if redactions else "")
        return len(chunks)

    def mark(self, doc_id: str, *, source: str, title: str, location: str, version: str, status: str,
             error: str, container: str | None = None) -> None:
        """Records a document that could not be indexed (unsupported or error)."""
        self.store.delete_doc(doc_id)
        self.state.upsert(doc_id, source=source, title=title, location=location, version=version,
                          status=status, error=error[:500], container=container)

    def remove(self, doc_id: str) -> None:
        self.store.delete_doc(doc_id)
        self.state.delete(doc_id)
        log.info("Removed from the index: %s", doc_id)
