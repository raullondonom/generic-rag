"""Redaction of secrets and personal data before indexing or sending text to Claude.

Two levels, configured separately:
- secrets (REDACT_SECRETS): API keys, tokens, private keys, passwords, connection strings and
  high-entropy values that look like credentials.
- personal data (REDACT_PERSONAL_DATA): emails, card numbers and ID or account numbers.

Patterns match both English and Spanish wording ("password" / "contraseña", "phone" / "teléfono").
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

DEFAULT_MARKER = "[REDACTED]"

_FULL: list[tuple[str, re.Pattern[str]]] = [
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----(?:(?!-----END )[\s\S])*"
            r"(?:-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----)?"
        ),
    ),
    ("anthropic", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{32,}")),
    ("stripe", re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA|AIPA)[A-Z0-9]{16}\b")),
    ("github", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("gitlab", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}")),
    ("slack", re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}")),
    ("google", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}")),
    ("huggingface", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
    ("atlassian", re.compile(r"\bATATT[A-Za-z0-9_\-=]{40,}")),
    ("vault", re.compile(r"\b(?:hvs|hvb|hvr)\.[A-Za-z0-9_\-]{20,}")),
    ("telegram", re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_\-]{33}\b")),
    ("npm", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    (
        "connection_string",
        re.compile(
            r"(?i)\b(?:server|data source|host|address|addr)\s*=[^;\n\"'`]+;"
            r"[^\n\"'`]*?\b(?:password|pwd)\s*=[^;\n\"'`]*;?"
        ),
    ),
    (
        "connection_string",
        re.compile(r"(?i)\bjdbc:[a-z0-9]+:[^\s\"'`]*?\b(?:password|pwd)=[^\s&;\"'`]+"),
    ),
]

_URL_CREDENTIALS = re.compile(r"\b([a-z][a-z0-9+.\-]*://)[^/\s:@\"'`]+:[^/\s@\"'`]+@", re.I)

_SECRET_WORDS = (
    r"password|passwd|passphrase|pwd|pass(?![a-z])|secret|token|api[_\-]?key|access[_\-]?key|"
    r"private[_\-]?key|client[_\-]?secret|credential|contrase(?:ñ|n)a|clave"
)

_KEY_VALUE = re.compile(
    r"(?i)(?P<pre>[\"']?\b[\w.\-]*(?:" + _SECRET_WORDS + r")[\w.\-]*[\"']?\s*(?::|=|=>)\s*)"
    r"(?P<value>\"[^\"\n]*\"|'[^'\n]*'|[^\s,;'\"}\])]+)"
)

_PHRASE = re.compile(
    r"(?i)(?P<pre>\b(?:la |el |mi |su )?(?:contrase(?:ñ|n)a|password|pass)\s+(?:es|era|sería|quedó en)\s+)"
    r"(?P<value>[\"']?[^\s\"']{4,}[\"']?)"
)

_HEADERS = [
    re.compile(r"(?i)(?P<pre>\bauthorization\s*:\s*(?:bearer|basic|token|digest)\s+)(?P<value>[A-Za-z0-9._~+/=\-]{8,})"),
    re.compile(r"(?i)(?P<pre>\b(?:bearer)\s+)(?P<value>[A-Za-z0-9._~+/=\-]{20,})"),
    re.compile(
        r"(?i)(?P<pre>\bx-(?:vault-token|api-key|auth-token|access-token|apikey)[\"']?\s*[:=]\s*[\"']?)(?P<value>[^\s\"',;}]+)"
    ),
]

_FLAGS = [
    re.compile(
        r"(?i)(?P<pre>(?<![\w-])--?(?:password|passwd|pass|token|secret|api-key|client-secret|access-key|secret-key)(?:=|\s+))"
        r"(?P<value>\"[^\"\n]*\"|'[^'\n]*'|[^\s\"']+)"
    ),
    re.compile(r"(?P<pre>\bsshpass\s+-p\s*)(?P<value>\"[^\"\n]*\"|'[^'\n]*'|\S+)"),
    re.compile(r"(?P<pre>(?<![\w-])-u\s+[\"']?[^\s:\"']+:)(?P<value>[^\s\"']+)"),
]

_DOCUMENTS = re.compile(
    r"(?i)(?P<pre>\b(?:c[ée]dula|c\.c\.|cc|dni|nit|rfc|curp|ssn|passport|pasaporte|documento(?: de identidad)?|"
    r"n[úu]mero de cuenta|cuenta(?: bancaria| de ahorros| corriente)?|account(?: number)?|"
    r"tarjeta|card|p[óo]liza|policy|tel[ée]fono|phone|celular|mobile)\b[^\d\n]{0,15}?)"
    r"(?P<value>\d[\d.\- ]{4,}\d)"
)

_CARD = re.compile(r"\b(?:\d{4}([ \-])\d{4}\1\d{4}\1\d{1,7}|\d{4}([ \-])\d{6}\2\d{5}|\d{15,16})\b")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_ENTROPY_CANDIDATE = re.compile(r"(?<![A-Za-z0-9+/_\-])[A-Za-z0-9+/_\-]{32,}={0,2}(?![A-Za-z0-9+/_\-])")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
_HEX = re.compile(r"^[0-9a-fA-F]+$")

_NON_SECRET_KEYS = re.compile(
    r"(?i)(?:tokens\b|token_count|max_token|tokenizer|palabras?\s+clave|clave_primaria|primary_key|"
    r"secret_?name|token_?type|token_?url|token_endpoint|"
    r"\.(?:cs|py|ts|js|tsx|jsx|json|md|ya?ml|java|go|sql|ps1|sh|txt|config|xml|csproj|vue|html)\b)"
)
_STRONG_KEY = re.compile(r"(?i)pass|pwd|secret|contrase|clave|private|credential|api[_\-]?key|access[_\-]?key")
_HARMLESS_VALUE = re.compile(r"^[\"']?(?:[A-Za-zÁÉÍÓÚáéíóúñÑ .]+|\d+)[\"']?$")

_REFERENCES = re.compile(
    r"^[\"']?(?:\$|%|<|\{\{|\[REDACTADO|\[REDACTED|process\.env|os\.environ|env:|getenv|"
    r"\*{3,}|x{3,}|null|none|true|false|undefined|string|str|int|\"\"|'')",
    re.I,
)


@dataclass
class RedactionResult:
    text: str
    counts: Counter = field(default_factory=Counter)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def _luhn(digits: str) -> bool:
    total, even = 0, False
    for d in reversed(digits):
        n = int(d)
        if even:
            n *= 2
            if n > 9:
                n -= 9
        total += n
        even = not even
    return total % 10 == 0


def _entropy(s: str) -> float:
    freq = Counter(s)
    return -sum(c / len(s) * math.log2(c / len(s)) for c in freq.values())


def _mixed(segment: str) -> bool:
    return bool(re.search(r"[A-Za-z]", segment) and re.search(r"\d", segment))


def _looks_secret(s: str) -> bool:
    core = s.rstrip("=")
    if _UUID.match(core) or _HEX.match(core):
        return False
    if not (re.search(r"[a-z]", core) and re.search(r"[A-Z]", core) and re.search(r"\d", core)):
        return False
    segments = [p for p in re.split(r"[_\-./+]", core) if p]
    mixed = [p for p in segments if _mixed(p)]
    mixed_length = sum(len(p) for p in mixed)
    if not any(len(p) >= 16 for p in mixed) and mixed_length < 0.6 * len(core):
        return False
    return _entropy(core) >= 4.0


def _is_card(candidate: str) -> bool:
    digits = re.sub(r"\D", "", candidate)
    return 13 <= len(digits) <= 19 and len(set(digits)) > 1 and _luhn(digits)


def _value_is_secret(m: re.Match[str], name: str) -> bool:
    value = m.group("value")
    if not value.strip("\"'") or _REFERENCES.match(value):
        return False
    before = re.split(r"[,;:.\n()\[\]{}]", m.string[max(0, m.start() - 12):m.start()])[-1]
    if _NON_SECRET_KEYS.search(before + m.group("pre")):
        return False
    if name == "key_value" and not _STRONG_KEY.search(m.group("pre")) and _HARMLESS_VALUE.match(value):
        return False
    return True


def _replace_full(name: str, pattern: re.Pattern[str], text: str, counts: Counter, marker: str) -> str:
    text, n = pattern.subn(marker, text)
    counts[name] += n
    return text


def _replace_value(name: str, pattern: re.Pattern[str], text: str, counts: Counter, marker: str) -> str:
    def _replacement(m: re.Match[str]) -> str:
        if not _value_is_secret(m, name):
            return m.group(0)
        counts[name] += 1
        return m.group("pre") + marker

    return pattern.sub(_replacement, text)


def _replace_if(name: str, pattern: re.Pattern[str], criterion, text: str, counts: Counter, marker: str) -> str:
    def _replacement(m: re.Match[str]) -> str:
        if not criterion(m.group(0)):
            return m.group(0)
        counts[name] += 1
        return marker

    return pattern.sub(_replacement, text)


_SECRETS_BY_VALUE: list[tuple[str, re.Pattern[str]]] = (
    [("header", p) for p in _HEADERS]
    + [("flag", p) for p in _FLAGS]
    + [("key_value", _KEY_VALUE), ("phrase", _PHRASE)]
)


def redact(text: str | None, secrets: bool = True, personal: bool = False,
           marker: str = DEFAULT_MARKER) -> RedactionResult:
    if not text or not (secrets or personal):
        return RedactionResult(text or "")
    counts: Counter = Counter()
    if secrets:
        for name, pattern in _FULL:
            text = _replace_full(name, pattern, text, counts, marker)
        text, n = _URL_CREDENTIALS.subn(lambda m: m.group(1) + marker + "@", text)
        counts["url_credentials"] += n
        for name, pattern in _SECRETS_BY_VALUE:
            text = _replace_value(name, pattern, text, counts, marker)
    if personal:
        text = _replace_value("document", _DOCUMENTS, text, counts, marker)
        text = _replace_if("card", _CARD, _is_card, text, counts, marker)
        text = _replace_full("email", _EMAIL, text, counts, marker)
    if secrets:
        text = _replace_if("high_entropy", _ENTROPY_CANDIDATE, _looks_secret, text, counts, marker)
    return RedactionResult(text, +counts)


class Redactor:
    """Applies the REDACT_SECRETS / REDACT_PERSONAL_DATA settings."""

    def __init__(self, secrets: bool, personal: bool, marker: str = DEFAULT_MARKER):
        self.secrets = secrets
        self.personal = personal
        self.marker = marker

    @classmethod
    def from_settings(cls, settings) -> "Redactor":
        from .i18n import t

        return cls(settings.redact_secrets, settings.redact_personal_data, t(settings.language, "redaction_marker"))

    @property
    def enabled(self) -> bool:
        return self.secrets or self.personal

    def __call__(self, text: str) -> RedactionResult:
        return redact(text, self.secrets, self.personal, self.marker)
