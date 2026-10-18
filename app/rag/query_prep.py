"""Before searching: spelling, a self-contained question and one search query per topic.

A fast model fixes the spelling of the question, rewrites it self-contained and splits it into one
query per topic, in the terms the documents would use, so that each topic finds its documents (a
misspelled word or a question mixing two subjects otherwise pulls the search toward one of them).
The answering model gets the original question too and checks the interpretation. If the step fails,
the search runs with the question as typed.
"""

from __future__ import annotations

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


def from_model(data: dict | None, question: str, max_queries: int) -> Prepared:
    """Validates the model output; anything unusable falls back to the question as typed."""
    if not isinstance(data, dict):
        return Prepared(original=question, corrected=question, standalone=question)

    def text(key: str, limit: int = 1000) -> str:
        value = data.get(key)
        return value.strip()[:limit] if isinstance(value, str) else ""

    def items(key: str, n: int) -> list[str]:
        value = data.get(key)
        if not isinstance(value, list):
            return []
        return [v.strip()[:300] for v in value if isinstance(v, str) and v.strip()][:n]

    corrected = text("corrected") or question
    standalone = text("standalone") or corrected
    return Prepared(original=question, corrected=corrected, standalone=standalone,
                    queries=items("queries", max_queries), missing=items("missing", 5), ok=True)


def fuse(result_lists: list[list[Hit]], top_k: int, min_score: float, score_margin: float,
         max_per_doc: int) -> list[Hit]:
    """Reciprocal rank fusion of several searches, with at most `max_per_doc` chunks per document.

    Each search keeps only its hits above max(MIN_SCORE, its best score - SCORE_MARGIN), so a weak
    query cannot bring noise; a chunk found by several queries ranks higher.
    """
    scores: dict[tuple, float] = {}
    best: dict[tuple, Hit] = {}
    for hits in result_lists:
        if not hits:
            continue
        cutoff = max(min_score, hits[0].score - score_margin)
        for rank, hit in enumerate(hits):
            if hit.score < cutoff:
                break
            key = (hit.payload.get("doc_id"), hit.payload.get("chunk_index"))
            scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank + 1)
            if key not in best or hit.score > best[key].score:
                best[key] = hit
    per_doc: Counter = Counter()
    out: list[Hit] = []
    for key in sorted(scores, key=scores.get, reverse=True):
        if per_doc[key[0]] >= max_per_doc:
            continue
        per_doc[key[0]] += 1
        out.append(best[key])
        if len(out) >= top_k:
            break
    return out
