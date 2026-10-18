"""Contextual retrieval: before embedding, a fast model writes a short note that situates each chunk.

A chunk of a long document often mixes several subjects (a list of thirty rules, for example), so its
embedding and its words describe none of them well. The note says what the chunk establishes, in the
terms people search for and in the light of the collection's purpose (its search notes). The note is
embedded and full-text indexed together with the chunk, but the answering model still reads the
original text only, so a wrong note can hide or surface a chunk but never changes an answer.

Notes are cached in Postgres by content: reindexing, or a document that did not change, costs nothing.
Every call goes through the Claude spending cap; when the budget runs out the remaining chunks are
indexed without a note and the document is retried on the next sync.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import anthropic

from ..config import Settings
from ..db import Database
from ..i18n import normalize, t
from ..llm.spending import BudgetExhausted, SpendTracker
from ..prompts import build_context_message, context_prompt

log = logging.getLogger(__name__)

PROMPT_VERSION = "1"  # bump when the prompt changes: every note is written again
MAX_TOKENS = 200
PREVIOUS_CHARS = 800  # end of the previous chunk, so a chunk that starts mid-list knows where it is
CACHE = {"type": "ephemeral"}
VERSION_MARK = "+ctx:"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _optional(source) -> str:
    if not source:
        return ""
    try:
        return source() or ""
    except Exception:  # noqa: BLE001 - the collection texts are optional
        log.exception("Could not read a collection text for the chunk notes")
        return ""


def strip_mark(version: str) -> str:
    """Document version without the notes mark (the document still lacks some notes)."""
    return version.split(VERSION_MARK, 1)[0]


class Contextualizer:
    def __init__(self, settings: Settings, db: Database, spend: SpendTracker, client=None,
                 describe=None, guidance=None):
        """`describe` / `guidance`: callables returning the collection's description and search notes."""
        self.model = settings.context_model or settings.anthropic_fast_model
        self.language = normalize(settings.language)
        self.workers = max(1, settings.context_workers)
        self.db = db
        self.spend = spend
        self.describe = describe
        self.guidance = guidance
        self._client = client or anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=4)

    def system(self) -> str:
        return context_prompt(self.language, _optional(self.describe), _optional(self.guidance))

    def mark(self) -> str:
        """Added to every document version: changes when the notes would change (prompt, model, the
        collection's description or search notes), so the documents are indexed again."""
        return VERSION_MARK + _sha(f"{PROMPT_VERSION}|{self.model}|{self.system()}")[:10]

    def notes(self, title: str, context: str, items: list[tuple[str, str, str]]) -> list[str | None]:
        """One note per (section, previous text, chunk); None where it could not be written."""
        system = self.system()
        signature = _sha(f"{PROMPT_VERSION}|{self.model}|{system}")
        keys = [_sha(f"{signature}|{title}|{context}|{section}|{previous}|{piece}")
                for section, previous, piece in items]
        found = self._load(keys)
        todo = [i for i, key in enumerate(keys) if key not in found]
        if todo:
            stop = threading.Event()

            def write(i: int) -> str | None:
                if stop.is_set():
                    return None
                section, previous, piece = items[i]
                message = build_context_message(self.language, title, context, section, previous, piece)
                try:
                    return self._write(system, message)
                except BudgetExhausted as e:
                    if not stop.is_set():
                        log.warning("Chunk notes stopped, Claude budget exhausted: %s", e)
                    stop.set()
                except Exception:  # noqa: BLE001 - a chunk without a note is still indexed
                    log.exception("Could not write the note of a chunk of %s", title)
                return None

            with ThreadPoolExecutor(self.workers) as pool:
                written = list(pool.map(write, todo))
            new = {keys[i]: note for i, note in zip(todo, written) if note}
            self._save(new)
            found.update(new)
        return [found.get(key) for key in keys]

    def _write(self, system: str, message: str) -> str | None:
        self.spend.check(self.model, len(system) + len(message), MAX_TOKENS, t(self.language, "purpose_context"))
        final = self._client.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": system, "cache_control": CACHE}],
            messages=[{"role": "user", "content": message}],
            output_config={"effort": "low"},
        )
        self.spend.record(final, "context")
        if final.stop_reason == "refusal":
            return None
        text = " ".join(b.text for b in final.content if b.type == "text").strip()
        return " ".join(text.split())[:1000] or None

    def _load(self, keys: list[str]) -> dict[str, str]:
        if not keys:
            return {}
        with self.db.conn() as c:
            rows = c.execute("SELECT key, note FROM chunk_context WHERE key = ANY(%s)", (keys,)).fetchall()
        return {r["key"]: r["note"] for r in rows}

    def _save(self, notes: dict[str, str]) -> None:
        if not notes:
            return
        with self.db.conn() as c, c.cursor() as cur:
            cur.executemany("INSERT INTO chunk_context (key, note) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
                            list(notes.items()))
