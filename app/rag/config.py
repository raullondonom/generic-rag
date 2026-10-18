import re
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings

PACKAGE_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    """Settings read from environment variables (see .env.example)."""

    # Language of everything the user reads: UI, canned answers, prompts (en | es)
    language: str = "en"

    # Administration panel (/admin) and its API. Empty = only from this machine (not through ngrok)
    admin_token: str = ""

    # Default AI provider: auto = Claude if ANTHROPIC_API_KEY is set, otherwise Ollama
    llm_provider: str = "auto"

    # Ollama (local). Empty URL = disabled
    ollama_url: str = "http://ollama:11434"
    ollama_chat_model: str = "qwen3:4b-instruct-2507-q4_K_M"
    ollama_think: bool = False
    ollama_num_ctx: int = 12288
    ollama_num_gpu: int = -1  # model layers on the GPU; -1 = automatic
    ollama_with_claude: bool = False  # offer Ollama too when Claude is available

    # Embeddings (Hugging Face text-embeddings-inference, always local)
    embedding_url: str = "http://embeddings:80"
    embedding_model: str = "Qwen/Qwen3-Embedding-0.6B"
    embedding_instruction: str = (
        "Given a question from a user, retrieve the passages of internal documents that answer it"
    )
    embedding_batch: int = 32

    # Anthropic
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5-5"
    anthropic_fast_model: str = "claude-haiku-5-5"  # rewrites follow-up questions
    anthropic_effort: str = "medium"
    anthropic_max_tokens: int = 16000
    anthropic_fallbacks: bool = True
    monthly_budget_usd: float = 10.0
    tz: str = "UTC"  # time zone for the monthly budget cut-off
    dry_run: bool = False  # simulated Claude: no API calls, no spending

    # Redaction before indexing and before sending text to Claude
    redact_secrets: bool = True
    redact_personal_data: bool = False

    # State
    database_url: str = "postgresql://rag:rag@postgres:5432/rag"
    conversation_retention_days: int = 180  # 0 = never deleted

    # Retrieval
    qdrant_url: str = "http://qdrant:6333"
    collection_prefix: str = "documents"
    top_k: int = 10
    # Before searching, a fast model fixes the spelling, rewrites the question and writes one search
    # query per topic; the answering model checks that interpretation against the original
    query_preparation: bool = True
    search_queries_max: int = 3
    # Hybrid search: keywords (and their synonyms or official terms) from the preparation step, each one a
    # semantic search restricted to the chunks that contain it (full-text index of Qdrant, in LANGUAGE)
    keyword_search: bool = True
    search_keywords_max: int = 3
    # A keyword found in more than this share of the chunks is too generic to filter and is skipped
    keyword_max_share: float = 0.02
    max_chunks_per_doc: int = 2  # so a single long document cannot fill the whole context
    min_score: float = 0.45
    score_margin: float = 0.12
    chunk_size: int = 3000
    # Contextual retrieval: a fast Claude model writes a search note per chunk while indexing (cached
    # in Postgres; counts against MONTHLY_BUDGET_USD). Changing it, or the collection's search notes,
    # indexes everything again.
    contextual_indexing: bool = False
    context_model: str = ""  # empty = ANTHROPIC_FAST_MODEL
    context_workers: int = 8
    chunk_overlap: int = 400
    history_turns: int = 6

    # Statuses (lowercase, from the source registry) left out of the search, e.g. repealed documents
    exclude_statuses: str = "repealed,derogada,derogado"

    # Local folder
    docs_path: str = "/data/docs"
    folder_scan_interval: int = 60  # seconds; 0 = automatic updates off (manual only)
    max_file_mb: int = 100
    ocr_enabled: bool = False
    ocr_langs: str = "eng+spa"
    # Files or folders whose name starts with one of these are never indexed ("_" = metadata, archives)
    ignore_prefixes: str = ".,_,~$"
    # OCR text next to a scanned PDF ("<name>.ocr.txt" beside "<name>.pdf") is indexed instead of the PDF.
    # The control folder's "ocr_sidecar_suffix" overrides it; empty = off
    ocr_sidecar_suffix: str = ".ocr.txt"

    # Source control folder inside the documents folder (registries of where each document came from)
    control_dir: str = "_control"
    control_check_time: str = "16:00"  # daily check of the registries, HH:MM in TZ; empty = manual only
    control_check_online: bool = True  # also fingerprint the remote URLs (no AI, no reindex)
    control_check_workers: int = 6

    # Confluence Cloud (optional, off by default)
    confluence_enabled: bool = False
    confluence_url: str = ""
    confluence_email: str = ""
    confluence_token: str = ""
    confluence_spaces: str = ""
    confluence_include_attachments: bool = True
    confluence_max_attachment_mb: int = 50
    confluence_sync_time: str = "16:00"  # daily, HH:MM in TZ; empty = manual only

    log_level: str = "INFO"

    @field_validator("confluence_sync_time", "control_check_time")
    @classmethod
    def _check_daily_time(cls, value: str, info) -> str:
        value = value.strip()
        if value and not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", value):
            raise ValueError(f"{info.field_name.upper()} must be HH:MM (24 h) or empty")
        return value

    @staticmethod
    def _daily(value: str) -> tuple[int, int] | None:
        if not value:
            return None
        hour, minute = value.split(":")
        return int(hour), int(minute)

    @property
    def confluence_daily_sync(self) -> tuple[int, int] | None:
        """(hour, minute) of the daily Confluence sync, or None if it only runs manually."""
        return self._daily(self.confluence_sync_time)

    @property
    def control_daily_check(self) -> tuple[int, int] | None:
        """(hour, minute) of the daily source control check, or None if it only runs manually."""
        return self._daily(self.control_check_time)

    @property
    def ignore_prefix_list(self) -> tuple[str, ...]:
        return tuple(p.strip() for p in self.ignore_prefixes.split(",") if p.strip())

    @property
    def excluded_status_list(self) -> list[str]:
        return [s.strip().lower() for s in self.exclude_statuses.split(",") if s.strip()]

    @property
    def confluence_space_keys(self) -> list[str]:
        return [s.strip() for s in self.confluence_spaces.split(",") if s.strip()]

    @property
    def collection_name(self) -> str:
        """One collection per embeddings model: changing the model never mixes vectors."""
        return f"{self.collection_prefix}__{re.sub(r'[^a-z0-9]+', '-', self.embedding_model.lower()).strip('-')}"

    @property
    def anthropic_enabled(self) -> bool:
        return bool(self.anthropic_api_key) or self.dry_run

    @property
    def default_provider(self) -> str:
        if self.llm_provider != "auto":
            return self.llm_provider
        return "anthropic" if self.anthropic_enabled else "ollama"


@lru_cache
def get_settings() -> Settings:
    return Settings()
