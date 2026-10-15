"""Troceo recursivo de texto: intenta cortar por títulos, párrafos, líneas, frases y palabras."""

from __future__ import annotations

import re

SEPARATORS = ["\n#", "\n\n", "\n", ". ", " "]


def _split(text: str, size: int, separators: list[str]) -> list[str]:
    if len(text) <= size:
        return [text]
    for i, sep in enumerate(separators):
        if sep not in text:
            continue
        pieces = text.split(sep)
        # Reponer el separador para no perder títulos ni puntuación
        pieces = [pieces[0]] + [sep.lstrip("\n") + p if sep.startswith("\n#") else p for p in pieces[1:]]
        joiner = "\n" if sep.startswith("\n#") else sep
        out: list[str] = []
        current = ""
        for piece in pieces:
            candidate = f"{current}{joiner}{piece}" if current else piece
            if len(candidate) <= size:
                current = candidate
                continue
            if current:
                out.append(current)
            if len(piece) > size:
                out.extend(_split(piece, size, separators[i + 1:]))
                current = ""
            else:
                current = piece
        if current:
            out.append(current)
        return out
    # Sin separadores útiles: corte duro
    return [text[j:j + size] for j in range(0, len(text), size)]


def chunk_text(text: str, size: int = 3000, overlap: int = 400) -> list[str]:
    """Divide el texto en fragmentos de hasta `size` caracteres con `overlap` de solape."""
    text = text.strip()
    if not text:
        return []
    base = [p.strip() for p in _split(text, size, SEPARATORS) if p.strip()]
    if overlap <= 0 or len(base) < 2:
        return base
    chunks = [base[0]]
    for prev, cur in zip(base, base[1:]):
        tail = prev[-overlap:]
        # Empezar el solape en un límite de palabra
        m = re.search(r"\s", tail)
        tail = tail[m.end():] if m else tail
        chunks.append(f"{tail}\n{cur}" if tail else cur)
    return chunks
