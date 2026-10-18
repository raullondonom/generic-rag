"""Indexing one document: sections -> redaction -> chunks -> embeddings -> Qdrant + state."""

from __future__ import annotations

import logging

from ..config import Settings
from ..embeddings import Embedder
from ..i18n import t
from ..redaction import Redactor
from ..sparse import document_vector
from ..vectorstore import VectorStore
from .chunker import chunk_text
from .contextual import PREVIOUS_CHARS, strip_mark
from .loaders import Section
from .state import StateStore

log = logging.getLogger(__name__)


class Indexer:
    def __init__(self, settings: Settings, embedder: Embedder, store: VectorStore, state: StateStore,
                 redactor: Redactor | None = None, contextualizer=None):
        """`contextualizer`: writes a search note per chunk (CONTEXTUAL_INDEXING), or None."""
        self.settings = settings
        self.contextualizer = contextualizer
        self.embedder = embedder
        self.store = store
        self.state = state
        self.redactor = redactor or Redactor.from_settings(settings)

    def is_current(self, doc_id: str, version: str) -> bool:
        """True if the document was already processed with that version (and did not end in error)."""
        doc = self.state.get(doc_id)
        return bool(doc and doc["version"] == version and doc["status"] != "error")

    def index(self, doc_id: str, *, source: str, title: str, location: str, url: str, version: str,
              sections: list[Section], container: str | None = None, context: list[str] | None = None,
              extra: dict | None = None) -> int:
        """`context`: lines added under the title of every chunk; `extra`: additional payload fields."""
        chunks: list[str] = []
        payloads: list[dict] = []
        items: list[tuple[str, str, str]] = []  # (section, end of the previous chunk, chunk) for the notes
        redactions = 0
        context_text = "".join(f"\n{line}" for line in context or [])
        for section in sections:
            # Redact before chunking: what reaches Qdrant (and later the AI) is already clean
            result = self.redactor(section.text)
            redactions += result.total
            header = title + (f" ({section.location})" if section.location else "") + context_text
            previous = ""
            for piece in chunk_text(result.text, self.settings.chunk_size, self.settings.chunk_overlap):
                # Title and context go inside the chunk: better search and context for the AI
                chunks.append(f"{header}\n\n{piece}")
                items.append((section.location or "", previous[-PREVIOUS_CHARS:], piece))
                previous = piece
                payloads.append({
                    **(extra or {}),
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

        to_embed = chunks
        if self.contextualizer is not None:
            notes = self.contextualizer.notes(title, context_text, items)
            for payload, note in zip(payloads, notes):
                if note:
                    payload["search_note"] = note
            # The note is embedded (and full-text indexed) with the chunk; the AI reads the chunk only
            to_embed = [f"{note}\n\n{chunk}" if note else chunk for chunk, note in zip(chunks, notes)]
            if any(note is None for note in notes):
                version = strip_mark(version)  # retried on the next sync (the notes written are cached)
        vectors = self.embedder.embed_documents(to_embed)
        # BM25 of the same text as the embedding (chunk and note): the lexical half of the hybrid search
        lexical = ([document_vector(text, self.settings.language) for text in to_embed]
                   if self.settings.sparse_search else None)
        self.store.upsert(doc_id, chunks, vectors, payloads, sparse=lexical)
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
