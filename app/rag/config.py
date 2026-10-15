from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Configuración leída de variables de entorno (ver .env.example)."""

    # Proveedor de IA
    llm_provider: str = "ollama"

    # Ollama
    ollama_url: str = "http://ollama:11434"
    ollama_chat_model: str = "qwen3:8b"
    ollama_think: bool = False
    ollama_num_ctx: int = 12288
    ollama_num_gpu: int = -1  # capas del modelo en GPU; -1 = automático
    embed_model: str = "bge-m3"

    # Anthropic
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5-5"
    anthropic_effort: str = "medium"
    anthropic_max_tokens: int = 16000
    anthropic_fallbacks: bool = True

    # Recuperación
    qdrant_url: str = "http://qdrant:6333"
    collection_name: str = "documentos"
    top_k: int = 8
    min_score: float = 0.45
    score_margin: float = 0.12
    chunk_size: int = 3000
    chunk_overlap: int = 400
    history_turns: int = 6

    # Carpeta
    docs_path: str = "/data/docs"
    state_dir: str = "/data/state"
    folder_scan_interval: int = 60
    max_file_mb: int = 100
    ocr_enabled: bool = False
    ocr_langs: str = "spa+eng"

    # Confluence
    confluence_enabled: bool = False
    confluence_url: str = ""
    confluence_email: str = ""
    confluence_token: str = ""
    confluence_spaces: str = ""
    confluence_include_attachments: bool = True
    confluence_max_attachment_mb: int = 50
    confluence_sync_minutes: int = 60

    log_level: str = "INFO"

    @property
    def confluence_space_keys(self) -> list[str]:
        return [s.strip() for s in self.confluence_spaces.split(",") if s.strip()]

    @property
    def available_providers(self) -> list[str]:
        providers = ["ollama"]
        if self.anthropic_api_key:
            providers.append("anthropic")
        return providers


@lru_cache
def get_settings() -> Settings:
    return Settings()
