"""Retrieval evaluation: does the context sent to the AI contain the evidence each question needs?

It runs the app's own search path (preparation, hybrid search, selectors, fusion and reranking) for
every case of a dataset and checks the chunks that would reach the answering model. No answer is
generated: it measures retrieval only, which is what decides whether an answer can be right.

Dataset (YAML), kept with the collection (for example in its control folder):

    cases:
      - id: refund-deadline
        question: "How long do I have to ask for a refund?"
        context: {region: North}          # optional: selector id -> value (ui.json context_fields)
        expect:                           # every group must be found in the context
          - - {title: "Refund policy", text: ["days"]}   # alternatives: any of them finds the group
            - {text: ["refund", "30 days"]}

An alternative matches a chunk when `title` is part of its document title and every `text` piece is
in its text (case and accents ignored). Results go to bench/results/.

    docker compose exec rag-app python -m bench.retrieval_eval --dataset /docs/_control/retrieval_eval.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path

import yaml

from rag import branding
from rag.chat import ChatService
from rag.config import get_settings
from rag.control import ControlFolder
from rag.db import Database
from rag.embeddings import Embedder
from rag.llm import build_llms
from rag.llm.spending import SpendTracker
from rag.vectorstore import Hit, VectorStore

HERE = Path(__file__).resolve().parent


def fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", (text or "").casefold())
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).split())


def matches(alternative: dict, hit: Hit) -> bool:
    title = fold(alternative.get("title", ""))
    if title and title not in fold(hit.payload.get("title", "")):
        return False
    pieces = alternative.get("text") or []
    pieces = [pieces] if isinstance(pieces, str) else pieces
    text = fold(hit.text)
    return all(fold(p) in text for p in pieces)


def score_case(case: dict, hits: list[Hit]) -> dict:
    groups = case.get("expect") or []
    found, ranks = [], []
    for group in groups:
        rank = next((i + 1 for i, h in enumerate(hits) if any(matches(a, h) for a in group)), None)
        found.append(rank is not None)
        ranks.append(rank)
    useful = sum(any(matches(a, h) for g in groups for a in g) for h in hits)
    return {"recall": sum(found) / len(groups) if groups else 1.0, "complete": all(found), "ranks": ranks,
            "useful_chunks": useful, "chunks": len(hits)}


async def run(args) -> dict:
    settings = get_settings()
    db = Database(settings.database_url)
    spend = SpendTracker(db, settings.monthly_budget_usd, settings.tz, language=settings.language)
    llms = build_llms(settings, spend)
    embedder, store = Embedder(settings), VectorStore(settings)
    folder = ControlFolder.load(settings.docs_path, settings.control_dir)
    brand = branding.load(settings.docs_path, settings.control_dir, settings.language)
    chat = ChatService(settings, embedder, store, llms, conversations=None,
                       describe=lambda: f"{brand['name']}: {brand['tagline']}",
                       search_notes=folder.search_notes if folder else None,
                       notes=folder.assistant_notes if folder else None)
    llm = chat.pick_llm(args.provider)
    fields = branding.with_options(brand.get("context_fields", []), store.values)
    cases = yaml.safe_load(Path(args.dataset).read_text(encoding="utf-8"))["cases"]
    if args.only:
        cases = [c for c in cases if c["id"] in args.only.split(",")]
    spent_before = spend.summary()["month_usd"]

    results = []
    for case in cases:
        context = branding.resolve_context(fields, case.get("context") or {})
        start = time.perf_counter()
        prepared, queries, hits = await chat.search(llm, case["question"], [], context)
        seconds = time.perf_counter() - start
        result = {"id": case["id"], **score_case(case, hits), "seconds": round(seconds, 1),
                  "queries": queries, "keywords": prepared.keywords,
                  "titles": [h.payload.get("title", "")[:90] for h in hits]}
        results.append(result)
        mark = "OK " if result["complete"] else ("~  " if result["recall"] else "-- ")
        print(f"{mark}{case['id']:32s} recall {result['recall']:.2f}  ranks {result['ranks']}  "
              f"useful {result['useful_chunks']}/{result['chunks']}  {seconds:.1f}s", flush=True)

    n = len(results) or 1
    summary = {
        "label": args.label,
        "cases": len(results),
        "recall": round(sum(r["recall"] for r in results) / n, 3),
        "complete": round(sum(r["complete"] for r in results) / n, 3),
        "useful_share": round(sum(r["useful_chunks"] for r in results) / max(sum(r["chunks"] for r in results), 1), 3),
        "seconds": round(sum(r["seconds"] for r in results) / n, 1),
        "cost_usd": round(spend.summary()["month_usd"] - spent_before, 4),
        "settings": {k: getattr(settings, k) for k in (
            "top_k", "search_queries_max", "keyword_search", "max_chunks_per_doc", "contextual_indexing")},
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    out = HERE / "results" / f"retrieval-{datetime.now():%Y%m%d-%H%M%S}-{args.label or 'run'}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"Saved {out}")
    db.close()
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dataset", required=True, help="YAML file with the cases")
    parser.add_argument("--label", default="", help="name of this run in the results file")
    parser.add_argument("--only", default="", help="comma separated case ids")
    parser.add_argument("--provider", default=None, help="provider for the preparation step")
    args = parser.parse_args()
    summary = asyncio.run(run(args))
    sys.exit(0 if summary["cases"] else 1)


if __name__ == "__main__":
    main()
