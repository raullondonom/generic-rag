from __future__ import annotations

from typing import AsyncIterator, Protocol


class LLM(Protocol):
    """Common interface. No provider gets tools: they cannot search the internet."""

    name: str
    model: str
    remote: bool  # True if the text leaves the machine (it is redacted before sending)

    def stream(self, system: str, messages: list[dict]) -> AsyncIterator[str]:
        """Streams the answer (text fragments)."""
        ...

    async def complete(self, system: str, messages: list[dict]) -> str:
        """Short answer without streaming (used to rewrite the question)."""
        ...
