from __future__ import annotations

from typing import AsyncIterator, Protocol


class LLM(Protocol):
    """Interfaz común. Ningún proveedor recibe herramientas: no pueden buscar en internet."""

    name: str
    model: str

    def stream(self, system: str, messages: list[dict]) -> AsyncIterator[str]:
        """Genera la respuesta en streaming (fragmentos de texto)."""
        ...

    async def complete(self, system: str, messages: list[dict]) -> str:
        """Respuesta corta sin streaming (usada para reformular la pregunta)."""
        ...
