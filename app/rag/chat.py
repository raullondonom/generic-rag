"""Flujo de respuesta: reformular -> recuperar -> guardrail -> generar en streaming."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import AsyncIterator

from .config import Settings
from .embeddings import Embedder
from .llm.base import LLM
from .prompts import GREETING_ANSWER, NO_CONTEXT_ANSWER, REWRITE_PROMPT, SYSTEM_PROMPT, build_user_message
from .vectorstore import Hit, VectorStore

log = logging.getLogger(__name__)

GREETING_RE = re.compile(
    r"^\s*(hola|buen[oa]s( d[ií]as| tardes| noches)?|hey|hi|hello|gracias|muchas gracias|ok|vale)[\s!¡.,]*$",
    re.IGNORECASE,
)


def group_hits(hits: list[Hit]) -> list[dict]:
    """Agrupa los fragmentos por documento y numera los documentos para las citas [n]."""
    docs: dict[str, dict] = {}
    for hit in hits:
        p = hit.payload
        doc = docs.get(p["doc_id"])
        if doc is None:
            source_label = f"Confluence · {p.get('container')}" if p.get("source") == "confluence" else "Carpeta local"
            doc = docs[p["doc_id"]] = {
                "n": len(docs) + 1,
                "doc_id": p["doc_id"],
                "title": p.get("title", ""),
                "location": p.get("location", ""),
                "url": p.get("url", ""),
                "source": p.get("source", ""),
                "source_label": source_label,
                "score": round(hit.score, 3),
                "sections": [],
                "texts": [],
            }
        doc["texts"].append(hit.text)
        if p.get("section") and p["section"] not in doc["sections"]:
            doc["sections"].append(p["section"])
    return list(docs.values())


class ChatService:
    def __init__(self, settings: Settings, embedder: Embedder, store: VectorStore, llms: dict[str, LLM]):
        self.settings = settings
        self.embedder = embedder
        self.store = store
        self.llms = llms

    def pick_llm(self, provider: str | None) -> LLM:
        name = provider or self.settings.llm_provider
        if name not in self.llms:
            raise ValueError(f"Proveedor '{name}' no disponible. Disponibles: {', '.join(self.llms)}")
        return self.llms[name]

    async def _search_query(self, llm: LLM, question: str, history: list[dict]) -> str:
        if not history:
            return question
        convo = "\n".join(f"{m['role']}: {m['content'][:1500]}" for m in history[-4:])
        try:
            rewritten = await llm.complete(
                REWRITE_PROMPT, [{"role": "user", "content": f"Conversación:\n{convo}\n\nÚltima pregunta: {question}"}]
            )
            return rewritten or question
        except Exception:  # noqa: BLE001 - si falla, se busca con la pregunta original
            log.exception("No se pudo reformular la pregunta")
            return question

    async def retrieve(self, query: str) -> list[Hit]:
        vector = await asyncio.to_thread(self.embedder.embed_one, query)
        hits = await asyncio.to_thread(self.store.search, vector, self.settings.top_k)
        if not hits:
            return []
        # Umbral absoluto + margen relativo al mejor resultado (descarta coincidencias débiles)
        cutoff = max(self.settings.min_score, hits[0].score - self.settings.score_margin)
        return [h for h in hits if h.score >= cutoff]

    async def answer(self, question: str, history: list[dict], provider: str | None) -> AsyncIterator[tuple[str, object]]:
        """Genera eventos (tipo, dato): meta, sources, token, done."""
        llm = self.pick_llm(provider)
        history = [m for m in history if m.get("role") in ("user", "assistant") and m.get("content")]
        history = history[-2 * self.settings.history_turns:]
        while history and history[0]["role"] != "user":
            history = history[1:]
        yield "meta", {"provider": llm.name, "model": llm.model}

        if GREETING_RE.match(question):
            yield "sources", []
            yield "token", GREETING_ANSWER
            yield "done", {"guardrail": "greeting"}
            return

        query = await self._search_query(llm, question, history)
        hits = await self.retrieve(query)
        docs = group_hits(hits)
        yield "sources", [{k: d[k] for k in ("n", "title", "location", "url", "source", "source_label", "score", "sections")}
                          for d in docs]

        if not docs:
            # Guardrail: sin contexto relevante no se llama a la IA
            yield "token", NO_CONTEXT_ANSWER
            yield "done", {"guardrail": "no_context", "query": query}
            return

        messages = [*history, {"role": "user", "content": build_user_message(question, docs)}]
        async for text in llm.stream(SYSTEM_PROMPT, messages):
            yield "token", text
        yield "done", {"query": query}
