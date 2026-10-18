"""Recursive text splitting: tries to cut at headings, paragraphs, lines, sentences and words."""

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
        # Put the separator back so headings and punctuation are not lost
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
    # No useful separator: hard cut
    return [text[j:j + size] for j in range(0, len(text), size)]


def chunk_text(text: str, size: int = 3000, overlap: int = 400) -> list[str]:
    """Splits the text into chunks of up to `size` characters with `overlap` characters of overlap."""
    text = text.strip()
    if not text:
        return []
    base = [p.strip() for p in _split(text, size, SEPARATORS) if p.strip()]
    if overlap <= 0 or len(base) < 2:
        return base
    chunks = [base[0]]
    for prev, cur in zip(base, base[1:]):
        tail = prev[-overlap:]
        # Start the overlap at a word boundary
        m = re.search(r"\s", tail)
        tail = tail[m.end():] if m else tail
        chunks.append(f"{tail}\n{cur}" if tail else cur)
    return chunks
