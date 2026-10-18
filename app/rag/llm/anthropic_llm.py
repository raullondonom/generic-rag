"""Claude through the API, with a spending cap, a record of every call and prompt caching.

Caching: the system prompt and the conversation history are identical across turns; the retrieved
documents and the new question go last. Two cache points are marked: the end of the system prompt
and the last message of the history, so every turn reuses the previous ones.
"""

from __future__ import annotations

import logging
from typing import AsyncIterator

import anthropic

from ..config import Settings
from ..i18n import t
from .spending import CHARS_PER_TOKEN, SpendTracker, Usage

log = logging.getLogger(__name__)
FALLBACK_BETA = "server-side-fallback-2026-07-01"
CACHE = {"type": "ephemeral"}


def _chars(system: str, messages: list[dict]) -> int:
    return len(system) + sum(len(m["content"]) for m in messages)


def with_cache_points(system: str, messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """System and messages with cache_control at the end of the system and of the last history message."""
    system_blocks = [{"type": "text", "text": system, "cache_control": CACHE}]
    out = [dict(m) for m in messages]
    if len(out) >= 2:
        last_history = out[-2]
        last_history["content"] = [{"type": "text", "text": last_history["content"], "cache_control": CACHE}]
    return system_blocks, out


class AnthropicLLM:
    name = "anthropic"
    remote = True

    def __init__(self, settings: Settings, spend: SpendTracker, client=None):
        self.model = settings.anthropic_model
        self.fast_model = settings.anthropic_fast_model
        self.effort = settings.anthropic_effort
        self.max_tokens = settings.anthropic_max_tokens
        self.fallbacks = settings.anthropic_fallbacks
        self.language = settings.language
        self.spend = spend
        self._client = client or anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=3)

    def _stream_ctx(self, model: str, system: str, messages: list[dict], effort: str, max_tokens: int):
        system_blocks, cached_messages = with_cache_points(system, messages)
        kwargs = dict(
            model=model,
            max_tokens=max_tokens,
            system=system_blocks,
            messages=cached_messages,
            output_config={"effort": effort},
        )
        # Haiku has no server-side fallback
        if self.fallbacks and not model.startswith("claude-haiku"):
            # If Claude declines the request, the API retries it on the recommended fallback model
            return self._client.beta.messages.stream(**kwargs, betas=[FALLBACK_BETA], fallbacks="default")
        return self._client.messages.stream(**kwargs)

    async def stream(self, system: str, messages: list[dict]) -> AsyncIterator[str]:
        self.spend.check(self.model, _chars(system, messages), self.max_tokens, t(self.language, "purpose_answer"))
        async with self._stream_ctx(self.model, system, messages, self.effort, self.max_tokens) as stream:
            async for text in stream.text_stream:
                yield text
            final = await stream.get_final_message()
        cost = self.spend.record(final, "answer")
        log.info("Claude %s: USD %.4f (cache read %s tokens)", final.model, cost,
                 getattr(final.usage, "cache_read_input_tokens", 0))
        if final.stop_reason == "refusal":
            yield t(self.language, "refusal")
        elif final.stop_reason == "max_tokens":
            yield t(self.language, "truncated")

    async def complete(self, system: str, messages: list[dict]) -> str:
        self.spend.check(self.fast_model, _chars(system, messages), 2000, t(self.language, "purpose_rewrite"))
        async with self._stream_ctx(self.fast_model, system, messages, "low", 2000) as stream:
            final = await stream.get_final_message()
        self.spend.record(final, "rewrite")
        if final.stop_reason == "refusal":
            return ""
        return "".join(b.text for b in final.content if b.type == "text").strip()


class DryRunLLM:
    """DRY_RUN=true: simulated Claude. Runs the same flow (budget, spend record) without calling the API."""

    name = "anthropic"
    remote = True

    def __init__(self, settings: Settings, spend: SpendTracker):
        self.model = f"{settings.anthropic_model} (dry run)"
        self._real_model = settings.anthropic_model
        self.max_tokens = settings.anthropic_max_tokens
        self.language = settings.language
        self.spend = spend

    async def stream(self, system: str, messages: list[dict]) -> AsyncIterator[str]:
        self.spend.check(self._real_model, _chars(system, messages), self.max_tokens, t(self.language, "purpose_answer"))
        docs = messages[-1]["content"].count("### ") if "<documents>" in messages[-1]["content"] else 0
        text = t(self.language, "dry_run", docs=docs) + (" [1]." if docs else ".")
        for word in text.split(" "):
            yield word + " "
        usage = Usage(input_tokens=int(_chars(system, messages) / CHARS_PER_TOKEN),
                      output_tokens=int(len(text) / CHARS_PER_TOKEN))
        self.spend.record_usage(self._real_model, "answer", usage, 0.0, simulated=True)

    async def complete(self, system: str, messages: list[dict]) -> str:  # noqa: ARG002
        # Without a model the question cannot be rewritten: search with the last question as-is
        return messages[-1]["content"].rsplit(":", 1)[-1].strip()
