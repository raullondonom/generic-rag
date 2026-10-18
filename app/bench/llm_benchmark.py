"""Benchmark of local LLMs (Ollama) for the RAG answer step.

Every model answers the dataset questions with ALL corpus documents as context (shuffled per case,
so the cache cannot reuse the prompt), using the app's own system prompt and message format.
Scoring is deterministic, no AI judge: expected facts found, [n] citations, and saying "I can't find
it" when the answer is not in the documents. It also measures speed and VRAM.

Run it inside the app container, with Ollama up:

    docker compose run --rm rag-app python -m bench.llm_benchmark \
        --models qwen3:4b-instruct-2507-q4_K_M,gemma4:e4b-it-qat --dataset es
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path

import httpx
import yaml

from rag.config import Settings
from rag.i18n import t
from rag.prompts import build_user_message, system_prompt

HERE = Path(__file__).resolve().parent
THINK_RE = re.compile(r"<think>.*?</think>", re.S)
CITE_RE = re.compile(r"\[\d+(?:\s*,\s*\d+)*\]")


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def load_dataset(name: str) -> tuple[dict, list[dict]]:
    root = HERE / "datasets" / name
    data = yaml.safe_load((root / "cases.yaml").read_text(encoding="utf-8"))
    docs = []
    for path in sorted((root / "corpus").glob("*.md")):
        text = path.read_text(encoding="utf-8")
        title = text.splitlines()[0].lstrip("# ").strip()
        docs.append({"title": title, "text": text})
    return data, docs


def context_for(case_index: int, docs: list[dict], language: str) -> list[dict]:
    order = docs[:]
    random.Random(case_index).shuffle(order)
    return [{"n": i + 1, "title": d["title"], "source_label": t(language, "local_folder"), "texts": [d["text"]]}
            for i, d in enumerate(order)]


def score(case: dict, answer: str, refusal: list[re.Pattern]) -> dict:
    text = norm(answer)
    refused = any(p.search(text) for p in refusal)
    cited = bool(CITE_RE.search(answer))
    out = {"refused": refused, "cited": cited}
    facts = case.get("facts") or []
    if facts:
        found = [any(norm(v) in text for v in variants) for variants in facts]
        out["facts"] = sum(found) / len(found)
        out["missing"] = [variants[0] for variants, ok in zip(facts, found) if not ok]
    leaked = [f for f in case.get("forbidden", []) if norm(f) in text]
    out["forbidden"] = leaked
    language_ok = True
    if case.get("expect_language"):
        out["language"] = detect_language(answer)
        language_ok = out["language"] == case["expect_language"]
    if case["answerable"] is False:
        out["correct"] = refused and not leaked
    elif case["answerable"] == "partial":
        out["correct"] = out["facts"] == 1 and refused and not leaked
    else:
        out["correct"] = out["facts"] == 1 and cited and not leaked and language_ok
    return out


_STOPWORDS = {
    "en": {"the", "and", "of", "to", "is", "are", "per", "they", "must", "in", "for", "with", "days", "year"},
    "es": {"el", "la", "los", "las", "de", "del", "que", "y", "por", "con", "se", "para", "días", "año"},
}


def detect_language(text: str) -> str:
    words = re.findall(r"[a-záéíóúñ]+", text.lower())
    counts = {lang: sum(w in stop for w in words) for lang, stop in _STOPWORDS.items()}
    return max(counts, key=counts.get)


class Ollama:
    def __init__(self, url: str):
        self.http = httpx.Client(base_url=url, timeout=httpx.Timeout(900.0, connect=10.0))
        self.no_think: set[str] = set()

    def installed(self) -> set[str]:
        return {m["name"] for m in self.http.get("/api/tags").json().get("models", [])}

    def pull(self, model: str) -> None:
        print(f"  pulling {model} ...", flush=True)
        last = ""
        with self.http.stream("POST", "/api/pull", json={"model": model, "stream": True}) as r:
            for line in r.iter_lines():
                if not line:
                    continue
                data = json.loads(line)
                if data.get("error"):
                    raise RuntimeError(data["error"])
                status = data.get("status", "")
                if status != last and not status.startswith("pulling "):
                    print(f"    {status}", flush=True)
                    last = status

    def unload_all(self) -> None:
        for m in self.http.get("/api/ps").json().get("models", []):
            self.http.post("/api/generate", json={"model": m["name"], "keep_alive": 0})

    def memory(self, model: str) -> dict:
        for m in self.http.get("/api/ps").json().get("models", []):
            if m["name"] == model or m.get("model") == model:
                size, vram = m.get("size", 0), m.get("size_vram", 0)
                return {"size_gb": round(size / 1e9, 2), "vram_gb": round(vram / 1e9, 2),
                        "gpu_share": round(vram / size, 3) if size else 0.0}
        return {}

    def chat(self, model: str, system: str, user: str, options: dict) -> dict:
        body = {"model": model, "stream": True, "options": options,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if model not in self.no_think:
            body["think"] = False
        start = time.perf_counter()
        first = None
        parts: list[str] = []
        final: dict = {}
        with self.http.stream("POST", "/api/chat", json=body) as r:
            if r.status_code != 200:
                detail = r.read().decode(errors="replace")
                if "think" in detail and model not in self.no_think:
                    self.no_think.add(model)
                    return self.chat(model, system, user, options)
                raise RuntimeError(f"Ollama {r.status_code}: {detail[:300]}")
            for line in r.iter_lines():
                if not line:
                    continue
                data = json.loads(line)
                if data.get("error"):
                    raise RuntimeError(data["error"])
                content = data.get("message", {}).get("content") or ""
                if content:
                    if first is None:
                        first = time.perf_counter() - start
                    parts.append(content)
                if data.get("done"):
                    final = data
                    break
        total = time.perf_counter() - start
        ns = 1e9
        return {
            "answer": "".join(parts),
            "ttft_s": round(first if first is not None else total, 2),
            "total_s": round(total, 2),
            "load_s": round(final.get("load_duration", 0) / ns, 2),
            "prompt_tokens": final.get("prompt_eval_count", 0),
            "prompt_tps": round(final.get("prompt_eval_count", 0) / (final.get("prompt_eval_duration", 0) / ns), 1)
            if final.get("prompt_eval_duration") else None,
            "output_tokens": final.get("eval_count", 0),
            "gen_tps": round(final.get("eval_count", 0) / (final.get("eval_duration", 0) / ns), 1)
            if final.get("eval_duration") else None,
        }


def mean(values) -> float | None:
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 3) if values else None


def summarize(model: str, memory: dict, results: list[dict], error: str | None = None) -> dict:
    if error:
        return {"model": model, "error": error}
    answerable = [r for r in results if r["case"]["answerable"] is True]
    unanswerable = [r for r in results if r["case"]["answerable"] is False]
    partial = [r for r in results if r["case"]["answerable"] == "partial"]
    facts = mean([r["score"]["facts"] for r in answerable + partial])
    cites = mean([1.0 if r["score"]["cited"] else 0.0 for r in answerable])
    refusals = mean([1.0 if r["score"]["correct"] else 0.0 for r in unanswerable]
                    + [1.0 if r["score"]["refused"] else 0.0 for r in partial])
    false_refusals = mean([1.0 if r["score"]["refused"] else 0.0 for r in answerable])
    quality = round(0.55 * (facts or 0) + 0.15 * (cites or 0) + 0.30 * (refusals or 0)
                    - 0.10 * (false_refusals or 0), 3)
    return {
        "model": model,
        "quality": quality,
        "facts": facts,
        "citations": cites,
        "refusals": refusals,
        "false_refusals": false_refusals,
        "fully_correct": f"{sum(1 for r in results if r['score']['correct'])}/{len(results)}",
        "ttft_s": mean([r["metrics"]["ttft_s"] for r in results]),
        "answer_s": mean([r["metrics"]["total_s"] for r in results]),
        "gen_tps": mean([r["metrics"]["gen_tps"] for r in results]),
        "prompt_tps": mean([r["metrics"]["prompt_tps"] for r in results]),
        "prompt_tokens": mean([r["metrics"]["prompt_tokens"] for r in results]),
        **memory,
    }


def print_table(rows: list[dict]) -> None:
    cols = [("model", "Model"), ("quality", "Quality"), ("facts", "Facts"), ("citations", "Cites"),
            ("refusals", "Refusals"), ("false_refusals", "False ref."), ("fully_correct", "Correct"),
            ("ttft_s", "TTFT s"), ("answer_s", "Answer s"), ("gen_tps", "Gen tok/s"),
            ("prompt_tps", "Prompt tok/s"), ("vram_gb", "VRAM GB"), ("gpu_share", "On GPU")]
    print("\n| " + " | ".join(c[1] for c in cols) + " |")
    print("|" + "---|" * len(cols))
    for r in sorted(rows, key=lambda x: x.get("quality", -1), reverse=True):
        if r.get("error"):
            print(f"| {r['model']} | error: {r['error'][:80]} |")
            continue
        print("| " + " | ".join("" if r.get(k) is None else str(r.get(k)) for k, _ in cols) + " |")


def rescore(path: Path, meta: dict, refusal: list[re.Pattern]) -> int:
    report = json.loads(path.read_text(encoding="utf-8"))
    cases = {c["id"]: c for c in meta["cases"]}
    rows = []
    for entry in report["models"]:
        summary = entry["summary"]
        if summary.get("error"):
            rows.append(summary)
            continue
        results = []
        for r in entry["results"]:
            case = cases.get(r["id"])
            if case is None:  # case removed from the dataset since that run
                continue
            r["score"] = score(case, r["answer"], refusal)
            results.append({"case": case, "answer": r["answer"], "metrics": r["metrics"], "score": r["score"]})
        memory = {k: summary[k] for k in ("size_gb", "vram_gb", "gpu_share") if k in summary}
        entry["summary"] = summarize(summary["model"], memory, results)
        rows.append(entry["summary"])
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print_table(rows)
    return 0


def main() -> int:
    settings = Settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", help="comma-separated Ollama model tags")
    ap.add_argument("--rescore", metavar="RESULTS_JSON",
                    help="recompute the scores of a previous run with the current cases (no model is called)")
    ap.add_argument("--dataset", default="es")
    ap.add_argument("--ollama-url", default=settings.ollama_url or "http://ollama:11434")
    ap.add_argument("--num-ctx", type=int, default=settings.ollama_num_ctx)
    ap.add_argument("--num-gpu", type=int, default=-1, help="layers on the GPU (-1 = automatic)")
    ap.add_argument("--no-pull", action="store_true", help="skip models that are not downloaded")
    ap.add_argument("--out", default=str(HERE / "results" / f"llm-{datetime.now():%Y%m%d-%H%M%S}.json"))
    args = ap.parse_args()

    meta, docs = load_dataset(args.dataset)
    language = meta.get("language", "en")
    refusal = [re.compile(p) for p in meta["refusal_patterns"]]
    if args.rescore:
        return rescore(Path(args.rescore), meta, refusal)
    if not args.models:
        ap.error("--models is required (or use --rescore)")
    system = system_prompt(language)
    options = {"num_ctx": args.num_ctx, "temperature": 0.1}
    if args.num_gpu >= 0:
        options["num_gpu"] = args.num_gpu

    ollama = Ollama(args.ollama_url)
    report = {"dataset": args.dataset, "options": options, "started": datetime.now().isoformat(), "models": []}
    rows = []
    for model in [m.strip() for m in args.models.split(",") if m.strip()]:
        print(f"\n== {model}", flush=True)
        results: list[dict] = []
        try:
            if model not in ollama.installed():
                if args.no_pull:
                    raise RuntimeError("not downloaded")
                ollama.pull(model)
            ollama.unload_all()
            warm = ollama.chat(model, system, "Hola", options)
            print(f"  loaded in {warm['load_s']} s", flush=True)
            memory = ollama.memory(model)
            print(f"  memory: {memory}", flush=True)
            for i, case in enumerate(meta["cases"]):
                user = build_user_message(case["question"], context_for(i, docs, language), language)
                metrics = ollama.chat(model, system, user, options)
                answer = metrics.pop("answer")
                leaked_thinking = bool(THINK_RE.search(answer))
                answer = THINK_RE.sub("", answer).strip()
                s = score(case, answer, refusal)
                s["leaked_thinking"] = leaked_thinking
                results.append({"case": case, "answer": answer, "metrics": metrics, "score": s})
                print(f"  {case['id']:<18} {'OK ' if s['correct'] else 'BAD'} facts={s.get('facts', '-')} "
                      f"cite={s['cited']} refused={s['refused']} {metrics['total_s']} s", flush=True)
            summary = summarize(model, memory, results)
        except Exception as e:  # noqa: BLE001 - one broken model must not stop the benchmark
            print(f"  ERROR: {type(e).__name__}: {e}", flush=True)
            summary = summarize(model, {}, results, f"{type(e).__name__}: {e}")
        finally:
            try:
                ollama.unload_all()
            except Exception:  # noqa: BLE001
                pass
        rows.append(summary)
        report["models"].append({"summary": summary, "results": [
            {"id": r["case"]["id"], "question": r["case"]["question"], "answer": r["answer"],
             "score": r["score"], "metrics": r["metrics"]} for r in results]})
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print_table(rows)
    print(f"\nDetailed answers: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
