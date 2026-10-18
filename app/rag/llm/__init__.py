from ..config import Settings
from .base import LLM
from .spending import SpendTracker


def build_llms(settings: Settings, spend: SpendTracker) -> dict[str, LLM]:
    """Claude if ANTHROPIC_API_KEY is set (or simulated with DRY_RUN). Ollama if OLLAMA_URL is not empty and
    there is no Claude key (docker compose does not start Ollama then), unless OLLAMA_WITH_CLAUDE=true."""
    llms: dict[str, LLM] = {}
    if settings.anthropic_api_key:
        from .anthropic_llm import AnthropicLLM

        llms["anthropic"] = AnthropicLLM(settings, spend)
    elif settings.dry_run:
        from .anthropic_llm import DryRunLLM

        llms["anthropic"] = DryRunLLM(settings, spend)
    if settings.ollama_url and (not settings.anthropic_api_key or settings.ollama_with_claude):
        from .ollama_llm import OllamaLLM

        llms["ollama"] = OllamaLLM(settings)
    return llms
