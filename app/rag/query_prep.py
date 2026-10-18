"""Before searching: spelling, a self-contained question and one search query per topic.

A fast model fixes the spelling of the question, rewrites it self-contained and splits it into one
query per topic, in the terms the documents would use, so that each topic finds its documents (a
misspelled word or a question mixing two subjects otherwise pulls the search toward one of them).
The answering model gets the original question too and checks the interpretation. If the step fails,
the search runs with the question as typed.
"""

from __future__ import annotations

import unicodedata
from collections import Counter
from dataclasses import dataclass, field

from .vectorstore import Hit

RRF_K = 60


@dataclass
class Prepared:
    original: str
    corrected: str = ""
    standalone: str = ""
    queries: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    ok: bool = False

    def search_queries(self, limit: int) -> list[str]:
        """Original question first (in case the interpretation is wrong), then the prepared ones."""
        out: list[str] = []
        for q in [self.original, self.standalone, *self.queries]:
            q = (q or "").strip()
            if q and q.lower() not in {x.lower() for x in out}:
                out.append(q)
        return out[: limit + 2]


def from_model(data: dict | None, question: str, max_queries: int, max_keywords: int = 0) -> Prepared:
    """Validates the model output; anything unusable falls back to the question as typed."""
    if not isinstance(data, dict):
        return Prepared(original=question, corrected=question, standalone=question)

    def text(key: str, limit: int = 1000) -> str:
        value = data.get(key)
        return value.strip()[:limit] if isinstance(value, str) else ""

    def items(key: str, n: int, max_len: int = 300) -> list[str]:
        value = data.get(key)
        if not isinstance(value, list):
            return []
        out: list[str] = []
        for v in value:
            v = v.strip()[:max_len] if isinstance(v, str) else ""
            if v and v.lower() not in {x.lower() for x in out}:
                out.append(v)
        return out[:n]

    corrected = text("corrected") or question
    standalone = text("standalone") or corrected
    return Prepared(original=question, corrected=corrected, standalone=standalone,
                    queries=items("queries", max_queries), keywords=items("keywords", max_keywords, 80),
                    missing=items("missing", 5), ok=True)


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKD", text.casefold()).encode("ascii", "ignore").decode()


def query_for_keyword(queries: list[str], keyword: str) -> int:
    """Index of the shortest topic query that mentions the keyword, else of the self-contained question.

    `queries` is [original, self-contained, *topic queries], as `Prepared.search_queries` returns them.
    """
    k = _fold(keyword)
    mentions = [i for i, q in enumerate(queries) if i >= 2 and k in _fold(q)]
    if mentions:
        return min(mentions, key=lambda i: len(queries[i]))
    return min(1, len(queries) - 1)


def fuse(result_lists: list[list[Hit]], top_k: int, min_score: float, score_margin: float,
         max_per_doc: int, per_search: int = 1, priority: list[list[Hit]] | None = None,
         priority_slots: int = 0) -> list[Hit]:
    """Reciprocal rank fusion of several searches, with at most `max_per_doc` chunks per document.

    Each search keeps only its hits above max(MIN_SCORE, its best score - SCORE_MARGIN), so a weak
    query cannot bring noise; a chunk found by several queries ranks higher. First come up to
    `priority_slots` chunks of the `priority` searches (the keyword ones, most specific first), taken
    in turns and one per document; then the best `per_search` hits of every other search, so a topic
    found by a single search is not crowded out by the chunks the other searches share; the rest is
    filled by fused score.
    """
    scores: dict[tuple, float] = {}
    best: dict[tuple, Hit] = {}
    kept: list[list[tuple]] = []
    priority = priority or []
    for hits in [*priority, *result_lists]:
        keys: list[tuple] = []
        if hits:
            cutoff = max(min_score, hits[0].score - score_margin)
            for rank, hit in enumerate(hits):
                if hit.score < cutoff:
                    break
                key = (hit.payload.get("doc_id"), hit.payload.get("chunk_index"))
                scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank + 1)
                if key not in best or hit.score > best[key].score:
                    best[key] = hit
                keys.append(key)
        kept.append(keys)
    by_score = sorted(scores, key=scores.get, reverse=True)
    per_doc: Counter = Counter()
    chosen: list[tuple] = []

    def take(key: tuple) -> bool:
        if len(chosen) >= top_k or key in chosen or per_doc[key[0]] >= max_per_doc:
            return False
        per_doc[key[0]] += 1
        chosen.append(key)
        return True

    # Keyword searches in turns, one chunk per document, until their slots are used
    queues = [list(keys) for keys in kept[:len(priority)]]
    slots = min(priority_slots, top_k)
    while len(chosen) < slots and any(queues):
        for queue in queues:
            while queue and per_doc[queue[0][0]]:
                queue.pop(0)
            if queue and len(chosen) < slots:
                take(queue.pop(0))
    for key in [k for keys in kept[len(priority):] for k in keys[:per_search]] + by_score:
        take(key)
    # Final order by fused score (the most supported chunks first)
    return [best[k] for k in sorted(chosen, key=scores.get, reverse=True)]
