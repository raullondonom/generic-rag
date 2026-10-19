from ..config import Settings
from .base import LLM
from .spending import SpendTracker


def build_llms(settings: Settings, spend: SpendTracker) -> dict[str, LLM]:
    """Claude if ANTHROPIC_API_KEY is set (or simulated with DRY_RUN), DeepSeek if
    DEEPSEEK_API_KEY is set, and Ollama if OLLAMA_URL is not empty and there is no remote
    provider (unless OLLAMA_WITH_CLAUDE=true)."""
    llms: dict[str, LLM] = {}
    if settings.anthropic_api_key:
        from .anthropic_llm import AnthropicLLM

        llms["anthropic"] = AnthropicLLM(settings, spend)
    elif settings.dry_run:
        from .anthropic_llm import DryRunLLM

        llms["anthropic"] = DryRunLLM(settings, spend)
    if settings.deepseek_api_key:
        from .deepseek_llm import DeepSeekLLM

        llms["deepseek"] = DeepSeekLLM(settings, spend)
    remote = bool(settings.anthropic_api_key or settings.deepseek_api_key)
    if settings.ollama_url and (not remote or settings.ollama_with_claude):
        from .ollama_llm import OllamaLLM

        llms["ollama"] = OllamaLLM(settings)
    return llms
