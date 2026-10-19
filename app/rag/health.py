"""Health checks for every service."""

from __future__ import annotations

import time

import httpx


def probe(fn) -> dict:
    start = time.perf_counter()
    try:
        status = {"ok": True, **(fn() or {})}
    except Exception as e:  # noqa: BLE001
        status = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    status["ms"] = round((time.perf_counter() - start) * 1000)
    return status


def ready(services) -> dict:
    """Light check to take or leave traffic: only what an answer needs and can lose (Postgres)."""
    database = probe(services.db.ping)
    return {"ok": database["ok"], "role": services.settings.role, "database": database}


def check(services) -> dict:
    s = services.settings

    def database():
        services.db.ping()

    def qdrant():
        httpx.get(f"{s.qdrant_url}/readyz", timeout=5).raise_for_status()

    def embeddings():
        info = services.embedder.info()
        model = info.get("model_id")
        if model != s.embedding_model:
            raise RuntimeError(f"the service loads {model!r} but EMBEDDING_MODEL is {s.embedding_model!r}")
        return {"model": model, "max_input_length": info.get("max_input_length")}

    def index():
        return {"collection": s.collection_name, "chunks": services.store.count()}

    result = {
        "database": probe(database),
        "qdrant": probe(qdrant),
        "embeddings": probe(embeddings),
        "index": probe(index),
    }
    providers = {}
    if "anthropic" in services.chat.llms:
        providers["anthropic"] = probe(lambda: {"dry_run": s.dry_run, **services.spend.summary()})
    if "ollama" in services.chat.llms:
        providers["ollama"] = probe(lambda: _ollama(s.ollama_url, s.ollama_chat_model))
    if "deepseek" in services.chat.llms:
        providers["deepseek"] = probe(lambda: _deepseek(s.deepseek_url, s.deepseek_api_key, s.deepseek_model))
    return {
        "ok": all(v["ok"] for v in result.values()) and any(p["ok"] for p in providers.values()),
        "default_provider": s.default_provider,
        "services": result,
        "providers": providers,
    }


def _ollama(url: str, model: str) -> dict:
    r = httpx.get(f"{url}/api/tags", timeout=5)
    r.raise_for_status()
    names = {m["name"] for m in r.json().get("models", [])}
    wanted = model if ":" in model else f"{model}:latest"
    if wanted not in names:
        raise RuntimeError(f"model {model} is not downloaded (the ollama-init service pulls it)")
    return {"model": model}


def _deepseek(url: str, key: str, model: str) -> dict:
    r = httpx.get(f"{url.rstrip('/')}/models", headers={"Authorization": f"Bearer {key}"}, timeout=5)
    r.raise_for_status()
    names = {m.get("id") for m in r.json().get("data", [])}
    if model not in names:
        raise RuntimeError(f"model {model} is not offered by the API")
    return {"model": model}
