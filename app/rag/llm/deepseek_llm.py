"""DeepSeek through its OpenAI-compatible API (no SDK: httpx).

The provider is remote: the caller redacts the question and the history before sending them
(`LLM.remote`). Answers stream; search preparation and reranking use JSON mode.
"""

from __future__ import annotations

import json
import logging
from typing import AsyncIterator

import httpx

from ..config import Settings
from ..i18n import t
from .spending import CHARS_PER_TOKEN, SpendTracker, Usage

log = logging.getLogger(__name__)
PREPARE_MAX_TOKENS = 1500
PURPOSE_LABELS = {"prepare": "purpose_rewrite", "rerank": "purpose_rerank"}


def _chars(system: str, messages: list[dict]) -> int:
    return len(system) + sum(len(m["content"]) for m in messages)


class DeepSeekLLM:
    name = "deepseek"
    remote = True

    def __init__(self, settings: Settings, spend: SpendTracker, client: httpx.AsyncClient | None = None):
        self.model = settings.deepseek_model
        self.fast_model = settings.deepseek_fast_model
        self.max_tokens = settings.deepseek_max_tokens
        self.thinking = settings.deepseek_thinking
        self.language = settings.language
        self.spend = spend
        self._client = client or httpx.AsyncClient(
            base_url=f"{settings.deepseek_url.rstrip('/')}/",
            timeout=httpx.Timeout(600.0, connect=10.0),
            headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
        )

    @staticmethod
    def _body(model: str, system: str, messages: list[dict], **extra) -> dict:
        return {"model": model, "messages": [{"role": "system", "content": system}, *messages], **extra}

    @staticmethod
    def _usage(data: dict) -> Usage:
        usage = data.get("usage") or {}
        prompt = int(usage.get("prompt_tokens") or 0)
        hit = int(usage.get("prompt_cache_hit_tokens") or 0)
        miss = int(usage.get("prompt_cache_miss_tokens") or 0)
        # prompt_tokens includes both hits and misses. DeepSeek bills the hit part from cache.
        input_tokens = miss if miss else (max(prompt - hit, 0) if hit else prompt)
        return Usage(input_tokens=input_tokens,
                     output_tokens=int(usage.get("completion_tokens") or 0),
                     cache_read=hit)

    def _record(self, model: str, purpose: str, usage: Usage) -> None:
        try:
            cost = self.spend.prices.cost(model, usage)
            self.spend.record_usage(model, purpose, usage, cost, simulated=False)
            log.info("DeepSeek %s (%s): USD %.4f", model, purpose, cost)
        except Exception:  # noqa: BLE001 - a spend record must not break an answer
            log.exception("Could not record the DeepSeek spend")

    async def stream(self, system: str, messages: list[dict]) -> AsyncIterator[str]:
        self.spend.check(self.model, _chars(system, messages), self.max_tokens, t(self.language, "purpose_answer"))
        body = self._body(self.model, system, messages, stream=True, max_tokens=self.max_tokens,
                          temperature=0.1, stream_options={"include_usage": True},
                          thinking={"type": "enabled" if self.thinking else "disabled"})
        text, usage, called = "", None, False
        try:
            async with self._client.stream("POST", "chat/completions", json=body) as resp:
                if resp.status_code != 200:
                    detail = (await resp.aread()).decode(errors="replace")
                    raise RuntimeError(f"DeepSeek answered {resp.status_code}: {detail[:300]}")
                called = True
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        data = json.loads(payload)
                    except ValueError:
                        continue
                    if data.get("usage"):
                        usage = data["usage"]
                    for choice in data.get("choices") or []:
                        content = (choice.get("delta") or {}).get("content")
                        if content:
                            text += content
                            yield content
        finally:
            if called:
                parsed = self._usage({"usage": usage}) if usage is not None else Usage(
                    input_tokens=int(_chars(system, messages) / CHARS_PER_TOKEN),
                    output_tokens=int(len(text) / CHARS_PER_TOKEN),
                )
                self._record(self.model, "answer", parsed)

    async def complete_json(self, system: str, messages: list[dict], schema: dict,
                            purpose: str = "prepare") -> dict:
        """Short structured answer from the fast model (preparing or reranking the search)."""
        # JSON mode requires the word "json" in the prompt; the schema also pins the fields.
        system = f"{system}\n\nReturn only a valid JSON object (no markdown) for this schema:\n" \
                 f"{json.dumps(schema, ensure_ascii=False)}"
        self.spend.check(self.fast_model, _chars(system, messages), PREPARE_MAX_TOKENS,
                         t(self.language, PURPOSE_LABELS.get(purpose, "purpose_rewrite")))
        body = self._body(self.fast_model, system, messages, max_tokens=PREPARE_MAX_TOKENS,
                          temperature=0, response_format={"type": "json_object"},
                          thinking={"type": "disabled"})
        resp = await self._client.post("chat/completions", json=body)
        if resp.status_code != 200:
            raise RuntimeError(f"DeepSeek answered {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        self._record(self.fast_model, purpose, self._usage(data))
        content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        try:
            return json.loads(content)
        except ValueError:
            return {}
