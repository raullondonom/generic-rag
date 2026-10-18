# Generic RAG

A question-answering assistant that replies **only** with the information found in:

- the files of a local folder (PDF, Word, Excel, PowerPoint, CSV, HTML, Markdown, plain text, emails, OpenDocument, RTF, EPUB…), and
- optionally, the pages and attachments of **Confluence Cloud** spaces (off by default).

Answers come from **Claude** (Anthropic API) when an API key is available, or from a **local model** (Ollama, on GPU or CPU) otherwise. With a Claude key the local model container is not even started, so it uses no GPU or RAM. Embeddings are always computed locally with Hugging Face [text-embeddings-inference](https://github.com/huggingface/text-embeddings-inference).

The web UI is mobile first and also works in desktop browsers. Its texts, and the assistant's prompts, are available in English and Spanish (`LANGUAGE=en|es`).

## Assistant rules

- It answers only from the indexed documents and cites its sources as `[n]`.
- It has no internet access: the AI gets no tools, so it cannot search anywhere else.
- If the information is missing or the question is ambiguous, it says so and **asks the user** for what it needs.
- If no chunk reaches the minimum similarity (`MIN_SCORE`), the AI is not even called.
- Documents never leave the machine while indexing. When Claude is used, only the chunks relevant to each question are sent, after secrets are redacted.

## Features

- **Persistent conversations.** The history is stored in Postgres. The browser only keeps the ids of its own conversations (in `localStorage`), so it can reopen or delete them later. There are no user accounts: the conversation id (a random UUID) acts as the key, and the server never lists conversations it was not asked for.
- **Optional answer ratings.** Every answer can be rated thumbs up or down, with an optional reason and comment. Ratings are stored in Postgres, together with a copy of the question and answer, and they help find what to improve (see [Ratings](#ratings)).
- **Search preparation.** Before searching, a fast model (`ANTHROPIC_FAST_MODEL`, or the local model) fixes the spelling, rewrites the question self-contained (resolving references to the conversation) and splits it into one search per topic, in the terms the documents would use. It also returns keywords: the distinctive terms of the question and the synonyms or official terms the documents may use for them (an everyday word and its legal name, for example). Each keyword runs a semantic search restricted to the chunks that contain it (full-text index of Qdrant in `LANGUAGE`, without accents, stemmed and without stopwords; built on the stored chunks, nothing is re-embedded), so a document that names the thing literally is found even when the embeddings miss it. A keyword found in more than `KEYWORD_MAX_SHARE` of the chunks (2 %) is too generic to filter and is skipped; the others, rarest first, get up to half of `TOP_K`, one chunk per document, each searched with the topic query that mentions it. The searches run together with the question as typed and their results are fused (reciprocal rank fusion, where every search keeps at least its best hit, and at most `MAX_CHUNKS_PER_DOC` chunks per document). The answering model gets the original question, the interpretation and the searches, and checks them: if the interpretation changed the meaning, it answers the original question. `QUERY_PREPARATION=false` turns it off.
- **Hybrid search.** Every chunk has its embedding and a BM25 sparse vector (`SPARSE_SEARCH`, on by default): words without accents and stemmed in `LANGUAGE`, codes such as `C.11` or `D.2` kept whole, and the IDF applied by Qdrant. The semantic searches find paraphrases; BM25 finds the exact terms and codes the embeddings miss. Both are fused by rank, and BM25 alone never calls the AI: some semantic search must reach `MIN_SCORE`.
- **Contextual indexing (optional).** With `CONTEXTUAL_INDEXING=true`, a fast Claude model writes a short search note for every chunk while indexing: what it establishes (rule, article or code, sanction, deadline…) in the terms people search for, guided by the collection's search notes. A chunk that mixes many subjects (a list of thirty rules, for example) becomes findable by each of them. The note is embedded and full-text indexed with the chunk, but the answering model still reads the original text only, so a wrong note never changes an answer. Notes are cached in Postgres by content and go through the monthly budget; if it runs out, the documents left without notes are retried on the next sync.
- **Clean end-user page, separate administration.** Users only see the conversation, the sources and the ratings. The provider, Claude's spend, the index, the source review, the ratings summary and the documents with problems live in the administration panel at `/admin` (see [Administration](#administration)).
- **The collection's own look.** Name, icon, colors, welcome text, suggested questions, a disclaimer and optional selectors (for example a region or a product line) come from `ui.json` in the control folder, so each collection brings its identity (see [Look of the collection](#look-of-the-collection-uijson)). The page always shows that answers are written by an AI that may make mistakes, and that questions are stored.
- **Claude spending control.** Before each call the worst-case cost is estimated against a monthly cap (`MONTHLY_BUDGET_USD`), and every call is recorded with its tokens and cost. The administration panel shows the month's spend. Prompt caching reuses the system prompt and the conversation history between turns, the search is prepared with a cheaper model, and `DRY_RUN=true` simulates Claude without spending anything.
- **Secret redaction.** API keys, tokens, private keys, passwords and connection strings are replaced with `[REDACTED]` before indexing and before anything is sent to Claude. Personal data (emails, card or ID numbers) is optional.
- **One collection per embeddings model.** When `EMBEDDING_MODEL` changes, everything is reindexed from scratch at startup and the previous model's collection is deleted, so vectors from different models are never mixed.
- **Health checks.** `GET /health` checks the database, Qdrant, the embeddings service (it must load the configured model), the index and each AI provider.

## Architecture

```
documents/ ──┐                          ┌─> Qdrant (vectors)
             ├─> rag-app (FastAPI) ─────┼─> Postgres (state, conversations, ratings, spend)
Confluence ──┘   · ingestion            ├─> embeddings (TEI + Qwen3-Embedding-0.6B)
(optional)
                 · REST API + web UI    ├─> Claude API (if there is an API key)
                                        └─> Ollama (local model, "ollama" profile)
```

| Service | Role | Port (localhost only) |
|---|---|---|
| `rag-app` | API, web UI and ingestion | `8000` |
| `postgres` | Ingestion state, conversations, ratings and Claude spend | — |
| `qdrant` | Vector database | `6333` (dashboard at `/dashboard`) |
| `embeddings` | Local embeddings (CPU, or GPU with `docker-compose.gpu.yml`) | — |
| `ollama` | Local model (`ollama` profile, only without a Claude key) | `11434` |
| `ollama-init` | Pulls the local model and exits (same profile) | — |
| `ngrok` | Protected public tunnel (optional, `ngrok` profile) | `4040` (dashboard) |

## Hardware requirements

| Scenario | CPU | RAM | GPU | Disk | Answer time |
|---|---|---|---|---|---|
| **Minimum**: answers with Claude; embeddings on CPU | 4 x86-64 cores with AVX2 | 8 GB (6 GB for Docker) | Not needed | 10 GB | 5–15 s |
| **Local without GPU**: Ollama on CPU | 8+ cores with AVX2 | 16 GB (12 GB for Docker) | No | 20 GB | 30–90 s |
| **Recommended**: Ollama and embeddings on GPU | 8+ cores with AVX2 | 32 GB (16 GB for Docker) | NVIDIA with 8 GB of VRAM or more, driver ≥ 550 | 25 GB SSD | 3–10 s |

Notes:

- It runs on Windows 10/11 (Docker Desktop with WSL2), Linux and macOS. On Apple Silicon, Docker containers cannot use the GPU, so it behaves like "Local without GPU".
- **Windows**: limit WSL2 memory in `%USERPROFILE%\.wslconfig` (for example `memory=16GB`) and keep a page file of at least 16 GB. Under WSL2 the GPU also uses Windows commit memory; when it runs out you get `cudaMalloc failed: out of memory` even with free VRAM.
- To check the GPU in Docker: `docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi`.

## Quick start

```bash
git clone https://github.com/raullondonom/generic-rag.git
cd generic-rag
cp .env.example .env            # on Windows (PowerShell): copy .env.example .env
cp -r examples/documents/. documents/   # optional sample; on Windows: Copy-Item examples\documents\* documents -Recurse
docker compose up -d --build
docker compose logs -f ollama-init   # the first start downloads the models; wait until it finishes
```

`documents/` holds your own content and is never versioned: git ignores everything in it. The sample (a user guide and a source control folder) lives in `examples/documents/`.

1. Open <http://localhost:8000> (on a phone, use your computer's IP or the ngrok tunnel).
2. Ask about the sample document (`example-user-guide.md`), for example *"How do I add new documents?"*. The answer must cite source `[1]`.
3. Check the rules with an unrelated question, like *"Who won the 2022 World Cup?"*. It must say it has no such information and ask for more details.
4. Copy your own files into `documents/` (or point `DOCS_HOST_PATH` to any folder): they are indexed by themselves in under a minute.

Optional:

- **NVIDIA GPU**: uncomment `COMPOSE_PATH_SEPARATOR` and `COMPOSE_FILE` in `.env`, set `TEI_GPU_IMAGE` for your GPU architecture, and run `docker compose up -d`.
- **Claude**: set the API key (see [Secrets](#secrets)). Claude then answers and Ollama is not started. To keep both and choose in the UI: `COMPOSE_PROFILES=ollama,ollama-with-claude` and `OLLAMA_WITH_CLAUDE=true`.
- **Confluence**: set the `CONFLUENCE_*` variables in `.env` (see below). It syncs once a day (`CONFLUENCE_SYNC_TIME`, 16:00 by default) and on demand from the administration panel.
- **Only Claude, no local model**: remove `ollama` from `COMPOSE_PROFILES` and leave `OLLAMA_URL` empty.
- **Spanish UI and prompts**: `LANGUAGE=es`.

## Choosing the local model

`app/bench/llm_benchmark.py` compares Ollama models on your hardware with a fixed dataset: every question gets all the documents of a fictional company as context (with distractors), and the score is deterministic, with no AI judge. It counts expected facts found, `[n]` citations, and saying "I can't find it" when the answer is not in the documents. It also measures time to first token, tokens per second and VRAM.

With a Claude key Ollama is not running, so start it first: `COMPOSE_PROFILES=ollama,ollama-with-claude docker compose up -d ollama`.

```bash
docker compose run --rm -v ./app/bench/results:/app/bench/results rag-app \
  python -m bench.llm_benchmark --dataset es \
  --models qwen3:4b-instruct-2507-q4_K_M,qwen3.5:4b-q4_K_M,gemma4:e4b-it-qat
```

The included dataset (`app/bench/datasets/es`) is in Spanish. To add another language, copy the folder and translate `cases.yaml` and the corpus.

Results on an RTX 4070 Laptop (8 GB of VRAM, shared with the embeddings service), Spanish dataset, `OLLAMA_NUM_CTX=12288`:

| Model | Fully correct (of 20) | VRAM | On GPU | Avg. answer | Time to first token | Tokens/s |
|---|---|---|---|---|---|---|
| **`qwen3:4b-instruct-2507-q4_K_M`** (default) | **20** | 3.6 GB | 100 % | 2.6 s | 1.2 s | 60 |
| `qwen3:8b` | 20 | 6.0 GB | 100 % | 4.2 s | 2.5 s | 38 |
| `qwen3.5:9b-q4_K_M` | 20 | 5.6 GB | 88 % | 5.8 s | 2.2 s | 31 |
| `gemma4:e4b-it-qat` | 19 | 3.1 GB | 100 % | 2.5 s | 1.1 s | 55 |
| `qwen3.5:4b-q4_K_M` | 18 | 3.3 GB | 100 % | 2.8 s | 1.2 s | 58 |
| `gemma4:e2b-it-qat` | 18 | 1.8 GB | 100 % | 1.4 s | 0.6 s | 88 |
| `granite4.2:8b-q4_K_M` | 18 | 6.3 GB | 100 % | 6.7 s | 2.2 s | 35 |

Every model ignored the prompt injection planted in a document, preferred a newer notice over the outdated runbook, and said "not found" for the 3 questions with no answer. The differences came from answering an English question in Spanish (`gemma4`, `qwen3.5:4b`, `granite`), a missing computation (`gemma4:e2b`), and incomplete multi-document answers. `qwen3:4b-instruct-2507` is the default because it gets everything right at the speed of the small models, and it leaves VRAM free for the embeddings service. With less VRAM, `gemma4:e2b-it-qat` (1.8 GB) is the fastest option. Bigger models did not answer better on this dataset.

To recompute the scores of a saved run after editing `cases.yaml` (no model is called):

```bash
docker compose run --rm -v ./app/bench/results:/app/bench/results rag-app \
  python -m bench.llm_benchmark --dataset es --rescore /app/bench/results/<run>.json
```

## Secrets

They are not stored in the repository. Docker Compose reads them from the system environment variables or from `.env`, which is in `.gitignore`.

| Variable | Use |
|---|---|
| `ANTHROPIC_API_KEY` or `GENRIC_RAG_ANTHROPIC_API_KEY` | Claude API key. Optional: without it only the local model is offered |
| `POSTGRES_PASSWORD` or `GENRIC_RAG_POSTGRES_PASSWORD` | Postgres password (letters and digits). Only reachable inside the Docker network |
| `CONFLUENCE_EMAIL` / `CONFLUENCE_TOKEN`, or `JIRA_EMAIL` / `JIRA_TOKEN` | Only for Confluence: Atlassian email and API token (id.atlassian.com → Security → API tokens) |
| `NGROK_AUTHTOKEN` | Only to publish the app with ngrok |
| `HF_TOKEN` | Only for private or gated Hugging Face embedding models |

On Windows, after creating an environment variable, open a new terminal (or restart VS Code) so it is picked up.

## Configuration (`.env`)

Every option is documented in [.env.example](.env.example). The most important ones:

| Variable | Default | Description |
|---|---|---|
| `LANGUAGE` | `en` | Language of the UI, the canned answers and the prompts (`en` / `es`) |
| `LLM_PROVIDER` | `auto` | `auto` (Claude if there is a key, otherwise Ollama), `anthropic` or `ollama` |
| `ANTHROPIC_MODEL` / `ANTHROPIC_FAST_MODEL` | `claude-opus-5-5` / `claude-haiku-5-5` | Model for answers / model to prepare the search |
| `ADMIN_TOKEN` | — | Token of the administration panel (`/admin`). Without it, the panel only opens from this machine |
| `ANTHROPIC_EFFORT` | `medium` | Claude's reasoning depth |
| `MONTHLY_BUDGET_USD` | `10` | Monthly cap for Claude |
| `DRY_RUN` | `false` | Simulated Claude, no spending |
| `OLLAMA_CHAT_MODEL` | `qwen3:4b-instruct-2507-q4_K_M` | Local model (see the benchmark) |
| `OLLAMA_WITH_CLAUDE` | `false` | Also offer Ollama when Claude is available (start it with the `ollama-with-claude` profile) |
| `EMBEDDING_MODEL` | `Qwen/Qwen3-Embedding-0.6B` | Embeddings model (TEI) |
| `REDACT_SECRETS` / `REDACT_PERSONAL_DATA` | `true` / `false` | Redaction before indexing and before Claude |
| `CONVERSATION_RETENTION_DAYS` | `180` | Inactive conversations are deleted after this many days (0 = never) |
| `CONFLUENCE_ENABLED` | `false` | Turns Confluence Cloud sync on |
| `CONFLUENCE_URL` / `CONFLUENCE_SPACES` | — | For example `https://your-company.atlassian.net/wiki` / keys of the spaces to index (shown in the URL: `/wiki/spaces/<KEY>/`) |
| `CONFLUENCE_SYNC_TIME` | `16:00` | Daily Confluence sync time (HH:MM in `TZ`; empty = manual only). The first start syncs right away |
| `FOLDER_SCAN_INTERVAL` | `60` | Seconds between folder scans (`0` = automatic updates off; reindex from `/admin`) |
| `TOP_K` / `MIN_SCORE` | `10` / `0.45` | Retrieved chunks and minimum similarity |
| `QUERY_PREPARATION` / `SEARCH_QUERIES_MAX` | `true` / `3` | Prepare the search (spelling, self-contained question, one search per topic) / searches per question |
| `KEYWORD_SEARCH` / `SEARCH_KEYWORDS_MAX` | `true` / `3` | Hybrid search with the keywords of the preparation step / keywords per question |
| `KEYWORD_MAX_SHARE` | `0.02` | Keywords found in more chunks than this share are too generic and are skipped |
| `MAX_CHUNKS_PER_DOC` | `2` | Chunks of the same document in the context |
| `SPARSE_SEARCH` | `true` | BM25 sparse vectors for the hybrid search (changing it creates a new collection and reindexes) |
| `CONTEXTUAL_INDEXING` | `false` | A search note per chunk written by `ANTHROPIC_FAST_MODEL` while indexing (cached; turning it on, off or editing the search notes reindexes everything) |
| `IGNORE_PREFIXES` | `.,_,~$` | Files and folders starting with these are never indexed |
| `CONTROL_DIR` / `CONTROL_CHECK_TIME` | `_control` / `16:00` | Source control folder and daily review time (empty = manual) |
| `CONTROL_CHECK_ONLINE` | `true` | Also fingerprint the remote URLs in the review |
| `EXCLUDE_STATUSES` | `repealed,derogada,derogado` | Registry statuses left out of the search |
| `MAX_FILE_MB` | `100` | Bigger files are skipped (raise it for large manuals) |
| `OCR_ENABLED` | `false` | OCR (Tesseract) for scanned PDFs and images |

After changing `.env`, apply it with `docker compose up -d`.
If you change `CHUNK_SIZE`, `CHUNK_OVERLAP` or the redaction settings, reindex everything (see below). Changing `EMBEDDING_MODEL` reindexes by itself.

## Documents folder layout and source control

Any folder layout works, but these conventions make retrieval and maintenance better:

- **Topic folders in English, numbered**, and names without accents or spaces, e.g. `01_national_laws/`, `05_local_regulations/<department>/<municipality>/`. The relative folder path goes into the context of every chunk, so similar documents of different places (one decree per city, for example) are told apart.
- **The `_` prefix is reserved for what must not be indexed**: the control folder, archived versions (`_archive/`), history. Every file or folder starting with `IGNORE_PREFIXES` (`.`, `_`, `~$` by default) is skipped, so content folders must not start with `_`.
- **A control folder** (`_control/`, see the sample in [examples/documents/_control](examples/documents/_control)) records where every document came from:

| File | Content |
|---|---|
| `control_config.json` | Registry file patterns, CSV delimiter, `column_aliases` (to read registries with other column names, e.g. in Spanish), review periods, expiry warning window, `document_extensions`, `excluded_dirs`, `assistant_notes_file`, `search_notes_file`, `ocr_sidecar_suffix`, `index_fields` (registry columns copied into the index, `["status", "valid_until", "jurisdiction"]` by default: they can fill the selectors of `ui.json` and prioritize the search), `ui_file` (`ui.json` by default) |
| `ui.json` | Look of the collection's page (see below) |
| `sources_*.csv` | One row per document (`file` relative to the documents root) with `title`, `doc_type`, `number`, `year`, `issuer`, `scope`, `jurisdiction`, `topic`, `download_url`, `reference_url`, `method`, `issue_date`, `retrieved_date`, `status`, `valid_until`, `review_frequency`, `change_check`, `notes`. A row with an empty `file` records a place where nothing was found. |

What the app does with it:

1. **Enriches every document** with its registry row: the registry title instead of the file name, and jurisdiction, issuer, type, status and valid-until date in the chunk context. Sources show the status and link to both the local file and the official `reference_url`. Editing a row reindexes only that document.
2. **Follows the collection's own instructions**: the file named in `assistant_notes_file` (inside the control folder) is added to the assistant's system prompt, e.g. "national law prevails over local rules". It is trusted operator text and it is not indexed. The file named in `search_notes_file` guides the search preparation instead: how to read the collection's questions and what to look for besides the topic (for example, in a collection of regulations, always the applicable sanction and the deadline to appeal).
3. **Indexes OCR text instead of scanned PDFs**: a `<name>.ocr.txt` next to `<name>.pdf` (`OCR_SIDECAR_SUFFIX`, or `ocr_sidecar_suffix` in the config) is indexed in its place, split by its `--- Page n of N ---` markers, with the PDF's registry row; citations open the PDF.
4. **Leaves repealed documents out of the search** (`EXCLUDE_STATUSES`, matched against the lowercase `status`). Folders listed in `excluded_dirs` are never indexed.
5. **Reviews the sources every day** (`CONTROL_CHECK_TIME`, 16:00 by default; empty = only from the administration panel), without AI and **without downloading or reindexing anything**. It reports expired and soon-to-expire documents, reviews due by `review_frequency`, registered files missing on disk, places with no document, documents with no registry row, unreachable URLs and **remote sources whose content changed** (SHA-256 of the file, or of the visible text for HTML pages; ETag / Last-Modified avoid downloading unchanged files; the first check only stores fingerprints). The documents folder stays read only: the review state lives in Postgres. The administration panel shows the result; the full list is at `GET /api/control`, and `docker compose exec rag-app python -m rag.control check [--offline]` runs it from the command line.

## Look of the collection (`ui.json`)

The control folder can hold a `ui.json` with the identity of the collection's page. Every key is optional; what is missing falls back to the generic texts of `LANGUAGE`. See [examples/documents/_control/ui.json](examples/documents/_control/ui.json).

| Key | Content |
|---|---|
| `name` / `short_name` | Name of the page and of the installed app |
| `tagline` | One line under the name; it also tells the search preparation what the collection is about |
| `welcome_title` / `welcome_text` | Shown before the first question |
| `placeholder` | Text of the question box |
| `suggestions` | Up to 8 example questions, shown as buttons on the welcome screen |
| `disclaimer` | Notice shown at the foot of the page, next to the fixed AI and privacy notices |
| `colors` | `accent`, `accent_text`, `accent_dark`, `accent_text_dark` (hex), for light and dark mode |
| `icon` | Image file inside the control folder (`.svg`, `.png`, `.jpg`, `.webp`) |
| `context_fields` | Up to 4 optional selectors shown above the question box (see below) |

**Selectors (`context_fields`).** They let users state their case once (a region, a product line, a type of contract…) instead of repeating it in every question. Each one has an `id`, a `label`, an optional `placeholder` (the "any" choice) and either:

- `options`: a fixed list of values, or
- `field`: a column listed in `index_fields`; its values in the index become the options (minus `exclude_options`). With `"boost": true`, an extra search restricted to the chosen value brings its documents up **without hiding the rest** of the collection.

The choice always reaches the search preparation and the answering model as the user's context. Selectors are optional, and the choice is remembered in the browser only (`localStorage`); it is never stored on the server. The server only accepts values that the selector offers.

## Administration

The administration panel is at `/admin`. It is where the AI provider for every answer is chosen (users cannot choose it; the choice survives restarts), and where the panel shows Claude's spend, the index status with the reindex buttons, the source review, the ratings summary and the documents with errors or without text.

- With `ADMIN_TOKEN` set (or the `GENRIC_RAG_ADMIN_TOKEN` environment variable), the panel asks for it once and keeps it in that browser; the admin API expects it in the `X-Admin-Token` header.
- Without a token, the panel and its API only answer requests from this machine (localhost); requests through ngrok or from other machines are refused. Set a token before exposing the app.

## Several instances on one machine

To run one instance per document collection side by side, give each one its own `.env` with `COMPOSE_PROJECT_NAME` and free ports (`APP_PORT`, `QDRANT_PORT`, `OLLAMA_PORT`, `NGROK_PANEL_PORT`). Keep in mind that each instance starts its own embeddings service (about 1.5 GB of VRAM on GPU).

## Ratings

Below every answer there are two buttons, 👍 and 👎. They are optional. A 👎 opens a small form with a reason (incorrect, incomplete, didn't find the info, wrong sources, other) and a comment; both can be left empty. Clicking the same button again removes the rating.

Each rating keeps a copy of the question, the answer, the sources and the provider and model that answered, so it is not lost when the conversation is deleted or expires.

- The administration panel and `GET /api/feedback/summary?days=30` show only aggregated numbers: satisfaction, ratings by provider and model, and reasons.
- The detail (questions, answers and comments) is exported from the command line by whoever runs the server:

```bash
docker compose exec rag-app python -m rag.feedback --negative --days 30          # JSON
docker compose exec rag-app python -m rag.feedback --format csv > feedback.csv
docker compose exec rag-app python -m rag.feedback --format summary
```

Typical uses: a `not_found` answer often means a document is missing from the index or `MIN_SCORE` is too high; `bad_sources` points at retrieval (chunking, `TOP_K`, the embeddings model); `wrong` or `incomplete` on one model compared with another helps decide the local model or Claude's effort.

## External access with ngrok (testing)

The end-user page has no login, so the ngrok tunnel is protected with a user and password (`NGROK_BASIC_AUTH`). The administration panel also needs `ADMIN_TOKEN` to open through the tunnel.

1. Create the `NGROK_AUTHTOKEN` environment variable with your authtoken (dashboard.ngrok.com → *Your Authtoken*).
2. Set `NGROK_BASIC_AUTH=user:password` in `.env`. The password must have at least 8 characters.
3. Start the tunnel:
   ```bash
   docker compose --profile ngrok up -d
   ```
4. Find the public URL at <http://localhost:4040> or with `docker compose logs ngrok`.

To close it, run `docker compose stop ngrok`. Keep in mind that anyone who gets in through ngrok can query the indexed documents and spend Claude credits.

## REST API

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/config` | Look of the collection (`ui.json`), with the selectors and their options |
| `POST` | `/api/chat` | `{question, conversation_id?, context?}` → SSE stream with `meta` (includes the `conversation_id`), `sources`, `token`, `done` (includes the `message_id` and the `queries` searched) and `error` events. `context` maps selector ids to values; `provider` is honoured only for admin requests |
| `POST` | `/api/conversations/lookup` | `{ids: [...]}` → summary of those conversations |
| `GET` / `DELETE` | `/api/conversations/{id}` | Messages of a conversation (with their ratings) / delete it |
| `PUT` / `DELETE` | `/api/conversations/{id}/messages/{message_id}/feedback` | `{rating: 1\|-1, reason?, comment?}` / remove the rating |
| `GET` | `/api/file?path=` | Opens a cited file of the folder |
| `GET` | `/api/admin/status` | **Admin.** Index, providers, Claude spend and last sync |
| `PUT` | `/api/admin/provider` | **Admin.** `{provider}` → provider for every answer (`null` = back to `LLM_PROVIDER`) |
| `GET` | `/api/feedback/summary?days=30` | **Admin.** Aggregated ratings |
| `GET` | `/api/control?findings=200` | **Admin.** Control folder registries and the last source review |
| `POST` | `/api/control/check` | **Admin.** `{online: true}` → starts a source review (no AI, nothing downloaded or reindexed) |
| `GET` | `/api/documents?source=&status=` | **Admin.** Processed documents (`indexed`, `unsupported`, `error`, `empty`) |
| `POST` | `/api/reindex` | **Admin.** `{source: folder\|confluence\|all, full: false}` |
| `GET` | `/health` | Health of every service (200 or 503) |
| `GET` | `/docs` | Interactive documentation (Swagger) |

**Admin** routes need the `X-Admin-Token` header when `ADMIN_TOKEN` is set, or a request from this machine otherwise.

Example:

```bash
curl -N -X POST http://localhost:8000/api/chat -H "Content-Type: application/json" \
  -d '{"question": "How do I add new documents?"}'
```

## Operations

- **Incremental ingestion**: only new or modified files (by hash) and Confluence pages or attachments with a new version are processed. Whatever is deleted at the source is also removed from the index.
- **Reindex everything**: `POST /api/reindex {"source": "all", "full": true}`.
- **Unsupported files or errors**: they show in the administration panel and in `GET /api/documents?status=error`.
- **Tests**: `docker compose run --rm --no-deps rag-app pytest -q`. The tests that need Postgres run when `TEST_DATABASE_URL` is set (they use a temporary schema):
  ```bash
  docker compose run --rm -e TEST_DATABASE_URL=postgresql://rag:<password>@postgres:5432/rag rag-app pytest -q
  ```
- **`cudaMalloc failed: out of memory`**: the model does not fit in VRAM. Lower `OLLAMA_NUM_GPU` (layers on the GPU) or `OLLAMA_NUM_CTX` in `.env`, or pick a smaller model, and run `docker compose up -d`.

## License

This project is distributed under the [PolyForm Noncommercial License 1.0.0](LICENSE).

- ✅ **Personal, private, educational, research or non-profit use**: allowed and free, including modified versions.
- ❌ **Commercial use** (selling it, offering it as a paid service or using it to make money): **not allowed** without a commercial license. If you are interested, contact the author at <https://github.com/raullondonom>.

The binding legal text is the [LICENSE](LICENSE) file.
