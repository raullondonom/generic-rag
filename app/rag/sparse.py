"""BM25 sparse vectors for the lexical half of the hybrid search.

Embeddings understand paraphrases but often miss exact terms: codes ("C.11", "D.2"), article
numbers, names. Every chunk also gets a sparse vector with the BM25 term-frequency weight of its
words; Qdrant applies the IDF (modifier "idf") over the whole collection, so the score is BM25.

Words are lowercased, without accents and stemmed in the collection's language (Snowball); codes
with dots or dashes are kept whole and also split ("técnico-mecánica" -> the whole term, "tecnico",
"mecanica"). Term ids are a stable hash of the term.
"""

from __future__ import annotations

import re
import unicodedata
import zlib
from collections import Counter
from functools import lru_cache

import snowballstemmer

from .i18n import normalize

K1 = 1.2
B = 0.75
AVG_TERMS = 400  # typical terms per chunk (CHUNK_SIZE 3000); only shapes the length normalization
TOKEN = re.compile(r"[a-z0-9]+(?:[.\-/][a-z0-9]+)*")
LANGUAGES = {"en": "english", "es": "spanish"}

STOPWORDS = {
    "en": set("""a an and are as at be but by for from has have he her his i if in into is it its of on or our
        she so that the their them then there these they this to was we were what when which who will with
        you your not no do does did can could would should may might must shall than also been being""".split()),
    "es": set("""a al algo algunas algunos ante antes como con contra cual cuales cuando de del desde donde
        dos e el ella ellas ellos en entre era eran es esa esas ese eso esos esta estaba estado estan estar
        este esto estos fue fueron ha han hasta hay la las le les lo los mas me mi mis muy ni no nos o otra
        otras otro otros para pero por porque que quien se sea segun ser si sin sobre son su sus tambien
        tiene tienen todo todos tu un una unas uno unos y ya yo usted ustedes cada puede pueden debe deben""".split()),
}


def fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in text if not unicodedata.combining(c))


@lru_cache(maxsize=4)
def _stemmer(language: str):
    return snowballstemmer.stemmer(LANGUAGES.get(normalize(language), "english"))


def terms(text: str, language: str) -> list[str]:
    stop = STOPWORDS.get(normalize(language), set())
    stem = _stemmer(language)
    out: list[str] = []
    for token in TOKEN.findall(fold(text)):
        parts = re.split(r"[.\-/]", token)
        if len(parts) > 1:
            out.append(token)  # the code or compound as written ("c.11", "tecnico-mecanica")
        for part in parts:
            if len(part) < 2 and not part.isdigit():
                continue
            if part in stop:
                continue
            out.append(part if part.isdigit() else stem.stemWord(part))
    return out


def term_id(term: str) -> int:
    return zlib.crc32(term.encode("utf-8")) & 0x7FFFFFFF


def document_vector(text: str, language: str) -> tuple[list[int], list[float]]:
    """BM25 term-frequency part of every term of a chunk (the IDF is applied by Qdrant)."""
    counts = Counter(term_id(t) for t in terms(text, language))
    length = sum(counts.values()) or 1
    norm = K1 * (1 - B + B * length / AVG_TERMS)
    ids = sorted(counts)
    return ids, [counts[i] * (K1 + 1) / (counts[i] + norm) for i in ids]


def query_vector(text: str, language: str) -> tuple[list[int], list[float]]:
    """Every distinct term of the query with weight 1 (BM25 scores each term once)."""
    ids = sorted({term_id(t) for t in terms(text, language)})
    return ids, [1.0] * len(ids)
