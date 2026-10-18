from __future__ import annotations

import json
from typing import AsyncIterator

import httpx

from ..config import Settings


class OllamaLLM:
    name = "ollama"
    remote = False

    def __init__(self, settings: Settings):
        self.model = settings.ollama_chat_model
        self.think = settings.ollama_think
        self.options = {"num_ctx": settings.ollama_num_ctx, "temperature": 0.1}
        if settings.ollama_num_gpu >= 0:
            self.options["num_gpu"] = settings.ollama_num_gpu
        self._client = httpx.AsyncClient(base_url=settings.ollama_url, timeout=httpx.Timeout(600.0, connect=10.0))

    def _body(self, system: str, messages: list[dict], stream: bool, think: bool) -> dict:
        return {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "stream": stream,
            "think": think,
            "options": self.options,
        }

    async def stream(self, system: str, messages: list[dict]) -> AsyncIterator[str]:
        async with self._client.stream("POST", "/api/chat", json=self._body(system, messages, True, self.think)) as resp:
            if resp.status_code != 200:
                detail = (await resp.aread()).decode(errors="replace")
                raise RuntimeError(f"Ollama answered {resp.status_code}: {detail[:300]}")
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                data = json.loads(line)
                if data.get("error"):
                    raise RuntimeError(f"Ollama: {data['error']}")
                content = data.get("message", {}).get("content")
                if content:
                    yield content
                if data.get("done"):
                    break

    async def complete_json(self, system: str, messages: list[dict], schema: dict) -> dict:
        """Structured answer (Ollama's JSON schema output). {} if it is unusable."""
        body = {**self._body(system, messages, False, False), "format": schema}
        resp = await self._client.post("/api/chat", json=body)
        resp.raise_for_status()
        try:
            return json.loads(resp.json().get("message", {}).get("content", ""))
        except ValueError:
            return {}
