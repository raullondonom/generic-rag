"""Answer flow: history -> rewrite -> retrieve -> guardrail -> stream the answer -> save."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import AsyncIterator

from .config import Settings
from .embeddings import Embedder
from .i18n import t
from .llm.base import LLM
from .prompts import build_rewrite_message, build_user_message, rewrite_prompt, system_prompt
from .redaction import Redactor
from .vectorstore import Hit, VectorStore

log = logging.getLogger(__name__)

GREETING_RE = re.compile(
    r"^\s*(hola|buen[oa]s( d[ií]as| tardes| noches)?|hey|hi|hello|good (morning|afternoon|evening)|"
    r"gracias|muchas gracias|thanks|thank you|ok|okay|vale)[\s!¡.,]*$",
    re.IGNORECASE,
)
SOURCE_FIELDS = ("n", "title", "location", "url", "source", "source_label", "score", "sections",
                 "status", "valid_until", "jurisdiction")


def group_hits(hits: list[Hit], language: str = "en") -> list[dict]:
    """Groups the chunks by document and numbers the documents for the [n] citations."""
    docs: dict[str, dict] = {}
    for hit in hits:
        p = hit.payload
        doc = docs.get(p["doc_id"])
        if doc is None:
            source_label = (f"Confluence · {p.get('container')}" if p.get("source") == "confluence"
                            else t(language, "local_folder"))
            doc = docs[p["doc_id"]] = {
                "n": len(docs) + 1,
                "doc_id": p["doc_id"],
                "title": p.get("title", ""),
                "location": p.get("location", ""),
                "url": p.get("url", ""),
                "source": p.get("source", ""),
                "source_label": source_label,
                "score": round(hit.score, 3),
                "status": p.get("status", ""),
                "valid_until": p.get("valid_until", ""),
                "jurisdiction": p.get("jurisdiction", ""),
                "sections": [],
                "texts": [],
            }
        doc["texts"].append(hit.text)
        if p.get("section") and p["section"] not in doc["sections"]:
            doc["sections"].append(p["section"])
    return list(docs.values())


class ChatService:
    def __init__(self, settings: Settings, embedder: Embedder, store: VectorStore, llms: dict[str, LLM],
                 conversations, redactor: Redactor | None = None):
        self.settings = settings
        self.language = settings.language
        self.embedder = embedder
        self.store = store
        self.llms = llms
        self.conversations = conversations
        self.redactor = redactor or Redactor.from_settings(settings)

    def pick_llm(self, provider: str | None) -> LLM:
        name = provider or self.settings.default_provider
        if name not in self.llms:
            available = ", ".join(self.llms) or "none"
            raise ValueError(f"Provider '{name}' is not available. Available: {available}")
        return self.llms[name]

    async def _search_query(self, llm: LLM, question: str, history: list[dict]) -> str:
        if not history:
            return question
        convo = "\n".join(f"{m['role']}: {m['content'][:1500]}" for m in history[-4:])
        try:
            rewritten = await llm.complete(
                rewrite_prompt(self.language),
                [{"role": "user", "content": build_rewrite_message(convo, question, self.language)}],
            )
            return rewritten or question
        except Exception:  # noqa: BLE001 - if it fails, search with the original question
            log.exception("Could not rewrite the question")
            return question

    async def retrieve(self, query: str) -> list[Hit]:
        vector = await asyncio.to_thread(self.embedder.embed_query, query)
        hits = await asyncio.to_thread(self.store.search, vector, self.settings.top_k,
                                       self.settings.excluded_status_list)
        if not hits:
            return []
        # Absolute threshold + margin relative to the best hit (drops weak matches)
        cutoff = max(self.settings.min_score, hits[0].score - self.settings.score_margin)
        return [h for h in hits if h.score >= cutoff]

    def _load_history(self, conversation_id: str) -> list[dict]:
        """Only complete question-answer pairs (a question whose answer failed is left out)."""
        rows = self.conversations.history(conversation_id, 2 * self.settings.history_turns + 1)
        history = []
        for prev, cur in zip(rows, rows[1:]):
            if prev["role"] == "user" and cur["role"] == "assistant":
                history += [prev, cur]
        return history[-2 * self.settings.history_turns:]

    def _clean(self, llm: LLM, text: str) -> str:
        """What leaves the machine (Claude) is redacted; documents were already redacted when indexed."""
        return self.redactor(text).text if llm.remote and self.redactor.enabled else text

    async def answer(self, question: str, conversation_id: str | None, provider: str | None
                     ) -> AsyncIterator[tuple[str, object]]:
        """Yields (kind, data) events: meta, sources, token, done. Saves the question and the answer."""
        llm = self.pick_llm(provider)
        if conversation_id and await asyncio.to_thread(self.conversations.exists, conversation_id):
            history = await asyncio.to_thread(self._load_history, conversation_id)
        else:
            conversation_id = await asyncio.to_thread(self.conversations.create, question.strip()[:80])
            history = []
        await asyncio.to_thread(self.conversations.add_message, conversation_id, "user", question)
        yield "meta", {"provider": llm.name, "model": llm.model, "conversation_id": conversation_id}

        answer, sources = "", []

        async def save(error: str | None = None, guardrail: str | None = None) -> int:
            return await asyncio.to_thread(self.conversations.add_message, conversation_id, "assistant", answer,
                                    sources=sources, provider=None if guardrail else llm.name,
                                    model=None if guardrail else llm.model, error=error)

        if GREETING_RE.match(question):
            answer = t(self.language, "greeting")
            yield "sources", []
            yield "token", answer
            mid = await save(guardrail="greeting")
            yield "done", {"guardrail": "greeting", "message_id": mid}
            return

        safe_question = self._clean(llm, question)
        safe_history = [{**m, "content": self._clean(llm, m["content"])} for m in history]
        query = await self._search_query(llm, safe_question, safe_history)
        docs = group_hits(await self.retrieve(query), self.language)
        sources = [{k: d[k] for k in SOURCE_FIELDS} for d in docs]
        yield "sources", sources

        if not docs:
            # Guardrail: without relevant context the AI is not called
            answer = t(self.language, "no_context")
            yield "token", answer
            mid = await save(guardrail="no_context")
            yield "done", {"guardrail": "no_context", "query": query, "message_id": mid}
            return

        user_message = build_user_message(safe_question, docs, self.language)
        messages = [*safe_history, {"role": "user", "content": user_message}]
        try:
            async for text in llm.stream(system_prompt(self.language), messages):
                answer += text
                yield "token", text
        except (Exception, asyncio.CancelledError) as e:
            # Also when the client closes the connection (CancelledError): save whatever there is
            await asyncio.shield(save(error=f"{type(e).__name__}: {e}"[:500]))
            raise
        mid = await save()
        yield "done", {"query": query, "message_id": mid}
