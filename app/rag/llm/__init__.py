from ..config import Settings
from .base import LLM


def build_llms(settings: Settings) -> dict[str, LLM]:
    from .ollama_llm import OllamaLLM

    llms: dict[str, LLM] = {"ollama": OllamaLLM(settings)}
    if settings.anthropic_api_key:
        from .anthropic_llm import AnthropicLLM

        llms["anthropic"] = AnthropicLLM(settings)
    return llms
