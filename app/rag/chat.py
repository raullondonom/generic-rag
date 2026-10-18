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
from . import query_prep, sparse
from .prompts import (PREPARE_SCHEMA, RERANK_SCHEMA, build_prepare_message, build_rerank_message, build_user_message,
                      prepare_prompt, rerank_prompt, system_prompt)
from .query_prep import Prepared
from .redaction import Redactor
from .vectorstore import Hit, VectorStore

log = logging.getLogger(__name__)

GREETING_RE = re.compile(
    r"^\s*(hola|buen[oa]s( d[ií]as| tardes| noches)?|hey|hi|hello|good (morning|afternoon|evening)|"
    r"gracias|muchas gracias|thanks|thank you|ok|okay|vale)[\s!¡.,]*$",
    re.IGNORECASE,
)
BM25_DEPTH = 15  # lexical hits per query that enter the fusion
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


def _optional(source, error: str) -> str:
    """Text from an optional collection setting (notes, description); "" if missing or broken."""
    if not source:
        return ""
    try:
        return source() or ""
    except Exception:  # noqa: BLE001 - these texts are optional
        log.exception(error)
        return ""


class ChatService:
    def __init__(self, settings: Settings, embedder: Embedder, store: VectorStore, llms: dict[str, LLM],
                 conversations, redactor: Redactor | None = None, notes=None, describe=None, search_notes=None):
        """`notes`: callable returning the collection administrator's instructions (control folder).
        `describe`: callable returning a one-line description of the collection, used to prepare searches.
        `search_notes`: callable returning how to read the collection's questions, for preparing searches."""
        self.settings = settings
        self.notes = notes
        self.describe = describe
        self.search_notes = search_notes
        self.language = settings.language
        self.embedder = embedder
        self.store = store
        self.llms = llms
        self.conversations = conversations
        self.redactor = redactor or Redactor.from_settings(settings)

    default_override: str | None = None  # provider picked in the admin panel

    def current_default(self) -> str | None:
        for name in (self.default_override, self.settings.default_provider):
            if name in self.llms:
                return name
        return next(iter(self.llms), None)

    def pick_llm(self, provider: str | None) -> LLM:
        name = provider or self.current_default() or self.settings.default_provider
        if name not in self.llms:
            available = ", ".join(self.llms) or "none"
            raise ValueError(f"Provider '{name}' is not available. Available: {available}")
        return self.llms[name]

    async def prepare(self, llm: LLM, question: str, history: list[dict], context: list[dict] | None = None
                      ) -> Prepared:
        """Spelling, a self-contained question and search queries; the question as typed if it fails."""
        if not self.settings.query_preparation:
            return query_prep.from_model(None, question, 0)
        max_keywords = self.settings.search_keywords_max if self.settings.keyword_search else 0
        convo = "\n".join(f"{m['role']}: {m['content'][:1500]}" for m in history[-4:])
        collection = _optional(self.describe, "Could not describe the collection")
        guidance = await asyncio.to_thread(_optional, self.search_notes, "Could not read the search notes")
        try:
            data = await llm.complete_json(
                prepare_prompt(self.language, collection, self.settings.search_queries_max, max_keywords, guidance),
                [{"role": "user", "content": build_prepare_message(convo, question, self.language, context)}],
                PREPARE_SCHEMA,
            )
        except Exception:  # noqa: BLE001 - if it fails, search with the question as typed
            log.exception("Could not prepare the search")
            data = None
        return query_prep.from_model(data or None, question, self.settings.search_queries_max, max_keywords)

    async def specific_keywords(self, keywords: list[str]) -> list[str]:
        """The keywords present in the index and specific enough to filter (rarest first)."""
        if not keywords or not self.settings.keyword_search:
            return []
        try:
            total = max(await asyncio.to_thread(self.store.count), 1)
            counted = [(await asyncio.to_thread(self.store.count_text, kw), kw) for kw in keywords]
        except Exception:  # noqa: BLE001 - without the full-text index the search is only semantic
            log.exception("Could not count the keyword matches")
            return []
        limit = self.settings.keyword_max_share * total
        return [kw for n, kw in sorted(counted, key=lambda x: x[0]) if 0 < n <= limit]

    async def retrieve(self, queries: list[str], boosts: list[dict[str, str]] | None = None,
                       keywords: list[str] | None = None, limit: int | None = None,
                       per_doc: int | None = None) -> list[Hit]:
        """One search per query, fused (reciprocal rank fusion) with at most MAX_CHUNKS_PER_DOC per document.

        Each boost ({indexed field: value}, from the user's selectors) adds searches restricted to it with
        the first two queries: its documents come up without hiding the rest of the collection. Each
        keyword adds a search among the chunks that contain it, with the query that mentions it (or the
        self-contained question); those searches get up to half of TOP_K.
        """
        if not queries:
            return []
        vectors = await asyncio.to_thread(self.embedder.embed_queries, queries)
        candidates = self.settings.top_k * 3
        excluded = self.settings.excluded_status_list
        results = [await asyncio.to_thread(self.store.search, v, candidates, excluded) for v in vectors]
        # A boosted search only brings chunks that compete with the best ones of the plain searches,
        # so the chosen value's documents about other subjects do not take the place of relevant ones
        bar = max((hits[0].score for hits in results if hits), default=0.0) - self.settings.score_margin
        for where in boosts or []:
            for v in vectors[:2]:
                hits = await asyncio.to_thread(self.store.search, v, candidates, excluded, where)
                results.append([h for h in hits if h.score >= bar])
        keyword_results = []
        for kw in keywords or []:
            vector = vectors[query_prep.query_for_keyword(queries, kw)]
            keyword_results.append(await asyncio.to_thread(self.store.search, vector, candidates, excluded,
                                                           None, kw))
        # Guardrail: if no semantic search reaches MIN_SCORE, BM25 alone does not call the AI
        if not any(hits and hits[0].score >= self.settings.min_score for hits in results + keyword_results):
            return []
        lexical = []
        if self.settings.sparse_search:
            for q in queries:
                lexical.append(await asyncio.to_thread(self.store.search_sparse,
                                                       sparse.query_vector(q, self.language), BM25_DEPTH, excluded))
        limit = limit or self.settings.top_k
        return query_prep.fuse(results, limit, self.settings.min_score, self.settings.score_margin,
                               per_doc or self.settings.max_chunks_per_doc, priority=keyword_results,
                               priority_slots=limit // 2, rank_only=lexical)

    async def rerank(self, llm: LLM, question: str, prepared: Prepared, context: list[dict],
                     hits: list[Hit]) -> list[Hit]:
        """The fast model scores every candidate (0-3); the useful ones (2+) reach the answer, at most
        TOP_K and MAX_CHUNKS_PER_DOC per document. On failure, the fused order is kept."""
        top_k, per_doc = self.settings.top_k, self.settings.max_chunks_per_doc
        excerpts = [(h.payload.get("title", "") + (f" ({h.payload['section']})" if h.payload.get("section") else ""),
                     h.payload.get("search_note", ""), h.text) for h in hits]
        scores: dict[int, int] = {}
        if len(hits) > 1:
            try:
                data = await llm.complete_json(
                    rerank_prompt(self.language),
                    [{"role": "user", "content": build_rerank_message(self.language, question, prepared.standalone,
                                                                      context, excerpts)}],
                    RERANK_SCHEMA, purpose="rerank")
                for item in (data or {}).get("scores", []):
                    n, score = item.get("n"), item.get("score")
                    if isinstance(n, int) and isinstance(score, int) and 1 <= n <= len(hits):
                        scores[n - 1] = max(0, min(3, score))
            except Exception:  # noqa: BLE001 - without the reranker the fused order is used
                log.exception("Could not rerank the search results")
        order = list(range(len(hits)))
        if scores:
            order.sort(key=lambda i: -scores.get(i, 0))  # stable: ties keep the fused order
            useful = [i for i in order if scores.get(i, 0) >= 2]
            # Nothing clearly useful: a few of the closest, so the answer can say what is missing
            order = useful or [i for i in order if scores.get(i, 0) >= 1][:3] or order[:3]
        chosen, count = [], {}
        for i in order:
            doc = hits[i].payload.get("doc_id")
            if count.get(doc, 0) < per_doc and len(chosen) < top_k:
                count[doc] = count.get(doc, 0) + 1
                chosen.append(hits[i])
        return chosen

    def _load_history(self, conversation_id: str) -> list[dict]:
        """Only complete question-answer pairs (a question whose answer failed is left out).
        With HISTORY_TURNS=0 nothing is carried over: every question is an independent query."""
        if self.settings.history_turns <= 0:
            return []
        rows = self.conversations.history(conversation_id, 2 * self.settings.history_turns + 1)
        history = []
        for prev, cur in zip(rows, rows[1:]):
            if prev["role"] == "user" and cur["role"] == "assistant":
                history += [prev, cur]
        return history[-2 * self.settings.history_turns:]

    def system(self) -> str:
        """System prompt plus the notes of the collection's administrator, if any (trusted operator text)."""
        notes = _optional(self.notes, "Could not read the assistant notes")
        base = system_prompt(self.language, self.settings.confluence_enabled, self.settings.answer_format,
                             independent=self.settings.history_turns <= 0)
        return f"{base}\n\n{t(self.language, 'corpus_notes')}\n{notes}" if notes else base

    def _clean(self, llm: LLM, text: str) -> str:
        """What leaves the machine (Claude) is redacted; documents were already redacted when indexed."""
        return self.redactor(text).text if llm.remote and self.redactor.enabled else text

    async def search(self, llm: LLM, question: str, history: list[dict], context: list[dict]
                     ) -> tuple[Prepared, list[str], list[Hit]]:
        """Preparation and retrieval of a question (also used by the retrieval evaluation)."""
        prepared = await self.prepare(llm, question, history, context)
        queries = prepared.search_queries(self.settings.search_queries_max)
        boosts = [{c["field"]: c["value"]} for c in context if c.get("field")]
        prepared.keywords = await self.specific_keywords(prepared.keywords)
        if not self.settings.rerank:
            return prepared, queries, await self.retrieve(queries, boosts, prepared.keywords)
        candidates = await self.retrieve(queries, boosts, prepared.keywords, limit=self.settings.rerank_candidates,
                                         per_doc=2 * self.settings.max_chunks_per_doc)
        return prepared, queries, await self.rerank(llm, question, prepared, context, candidates)

    async def replay(self, question: str, conversation_id: str | None, cached: dict
                     ) -> AsyncIterator[tuple[str, object]]:
        """Same events as `answer` for an answer that was already generated (cache): no search, no AI call.
        The conversation is saved as usual."""
        if conversation_id and await asyncio.to_thread(self.conversations.exists, conversation_id):
            cid = conversation_id
        else:
            cid = await asyncio.to_thread(self.conversations.create, question.strip()[:80])
        await asyncio.to_thread(self.conversations.add_message, cid, "user", question)
        yield "meta", {"provider": cached.get("provider"), "model": cached.get("model"), "conversation_id": cid,
                       "cached": True}
        yield "sources", cached["sources"]
        yield "token", cached["answer"]
        guardrail = cached.get("guardrail")
        mid = await asyncio.to_thread(
            self.conversations.add_message, cid, "assistant", cached["answer"], sources=cached["sources"],
            provider=None if guardrail else cached.get("provider"), model=None if guardrail else cached.get("model"))
        done = {"queries": cached.get("queries", []), "keywords": cached.get("keywords", []), "message_id": mid}
        if guardrail:
            done["guardrail"] = guardrail
        yield "done", done

    async def answer(self, question: str, conversation_id: str | None, provider: str | None,
                     context: list[dict] | None = None) -> AsyncIterator[tuple[str, object]]:
        """Yields (kind, data) events: meta, sources, token, done. Saves the question and the answer.
        `context`: the user's selectors ({label, value, field}); `field` set = prioritize that value."""
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
        context = context or []
        prepared, queries, hits = await self.search(llm, safe_question, safe_history, context)
        docs = group_hits(hits, self.language)
        sources = [{k: d[k] for k in SOURCE_FIELDS} for d in docs]
        yield "sources", sources

        if not docs:
            # Guardrail: without relevant context the AI is not called
            answer = t(self.language, "no_context")
            yield "token", answer
            mid = await save(guardrail="no_context")
            yield "done", {"guardrail": "no_context", "queries": queries, "keywords": prepared.keywords,
                           "message_id": mid}
            return

        user_message = build_user_message(safe_question, docs, self.language, prepared, context)
        messages = [*safe_history, {"role": "user", "content": user_message}]
        try:
            async for text in llm.stream(await asyncio.to_thread(self.system), messages):
                answer += text
                yield "token", text
        except (Exception, asyncio.CancelledError) as e:
            # Also when the client closes the connection (CancelledError): save whatever there is
            await asyncio.shield(save(error=f"{type(e).__name__}: {e}"[:500]))
            raise
        mid = await save()
        yield "done", {"queries": queries, "keywords": prepared.keywords, "message_id": mid}
