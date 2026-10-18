# Bump rules for this project

## MAJOR (breaking)
- Remove or rename an endpoint in `app/rag/main.py`, or change method/path.
- Remove a field from a response, change its type, or make a request field required.
- Remove or rename a variable in `.env.example`, or change its meaning/default so existing `.env` files behave differently.
- Postgres: drop/rename a table or column, change a type, add a NOT NULL column without default (needs manual migration).
- Change the embedding model, vector size or sparse vector format so the existing Qdrant collection is invalid and a manual reindex is required (document it under Breaking).
- Change the layout the control folder or `documents/` expects.
- Remove or rename a CLI module or flag (`python -m rag.control`, `bench.*`).

While `0.y.z`: the same changes bump MINOR instead; keep them under **Breaking** in the changelog.

## MINOR (feature)
- New endpoint, new optional response field, new optional env var with a safe default.
- Additive migration (new table, new nullable column).
- New CLI flag, new ingest source, new language, new provider.
- Deprecate something (still works, announce removal in the changelog).

## PATCH (fix)
- Bug fix, performance improvement, security fix with no interface change.
- Prompt or retrieval tuning that fixes wrong behavior without changing configuration.
- Dependency bumps without behavior change.

## No release needed
Docs, tests, CI, comments, formatting, repo tooling (skills, graphify). They accumulate and ship with the next release; they appear in the changelog only if user-relevant.

## Examples
| Change | Bump |
|---|---|
| `fix: citations pointed to wrong page` | PATCH |
| `feat(retrieval): hybrid BM25 search` (optional, on by default) | MINOR |
| `feat(api)!: /api/chat returns sources as objects` | MAJOR |
| `refactor: split chat.py` (no behavior change) | none |
| Remove `RERANK_MODEL` from `.env.example` | MAJOR |
