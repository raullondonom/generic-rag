from __future__ import annotations

import logging
from typing import AsyncIterator

import anthropic

from ..config import Settings

log = logging.getLogger(__name__)
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicLLM:
    name = "anthropic"

    def __init__(self, settings: Settings):
        self.model = settings.anthropic_model
        self.effort = settings.anthropic_effort
        self.max_tokens = settings.anthropic_max_tokens
        self.fallbacks = settings.anthropic_fallbacks
        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    def _stream_ctx(self, system: str, messages: list[dict], effort: str, max_tokens: int):
        kwargs = dict(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            output_config={"effort": effort},
        )
        if self.fallbacks:
            # Si Claude rechaza la petición, la API la reintenta en el modelo de respaldo recomendado
            return self._client.beta.messages.stream(**kwargs, betas=[FALLBACK_BETA], fallbacks="default")
        return self._client.messages.stream(**kwargs)

    async def stream(self, system: str, messages: list[dict]) -> AsyncIterator[str]:
        async with self._stream_ctx(system, messages, self.effort, self.max_tokens) as stream:
            async for text in stream.text_stream:
                yield text
            final = await stream.get_final_message()
        if final.stop_reason == "refusal":
            yield "\n\n_La solicitud fue rechazada por las políticas de seguridad del modelo._"
        elif final.stop_reason == "max_tokens":
            yield "\n\n_(Respuesta truncada: se alcanzó ANTHROPIC_MAX_TOKENS)_"

    async def complete(self, system: str, messages: list[dict]) -> str:
        async with self._stream_ctx(system, messages, "low", 2000) as stream:
            final = await stream.get_final_message()
        if final.stop_reason == "refusal":
            return ""
        return "".join(b.text for b in final.content if b.type == "text").strip()
