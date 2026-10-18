"""Source control folder: registries of where every document came from, and a daily review of them.

The format is the one of `_control/` (see README): a `control_config.json` and one or more CSV
registries (`sources_*.csv`), with column aliases so registries in other languages can be read.
The app reads it to enrich every indexed document (title, jurisdiction, status, official URL) and
to review the sources without AI: expired or expiring documents, reviews due, files missing on
disk, documents with no registry row, and remote URLs whose content changed. The documents folder
is mounted read only, so the review state lives in Postgres. Nothing is downloaded or reindexed.

    python -m rag.control check            # dates, local files and remote fingerprints
    python -m rag.control check --offline  # dates and local files only
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import html
import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

COLUMNS = (
    "file", "title", "doc_type", "number", "year", "issuer", "scope", "jurisdiction", "topic",
    "download_url", "reference_url", "method", "issue_date", "retrieved_date", "status",
    "valid_until", "review_frequency", "change_check", "notes",
)
DEFAULTS = {
    "registry_globs": ["sources_*.csv"],
    "master_file": "sources_master.csv",
    "csv_delimiter": ";",
    "column_aliases": {},
    "review_periods_days": {"monthly": 30, "quarterly": 91, "semiannual": 182, "annual": 365, "on_change": 180},
    "default_review_days": 180,
    "warn_days_before_expiry": 30,
    "document_extensions": [".pdf"],
    "excluded_dirs": ["_control", "_archive"],
    # Registry columns copied into the index: they can filter the search and fill the UI selectors
    "index_fields": ["status", "valid_until", "jurisdiction"],
    "user_agent": "Mozilla/5.0 (compatible; generic-rag source control)",
    "timeout_seconds": 40,
}
SECTIONS = ("expired", "expiring", "changed", "review_due", "missing", "unreachable", "no_document", "unregistered")
URL_RE = re.compile(r"https?://[^\s|]+")
FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
# Payload keys the indexer writes itself: a registry column cannot replace them
RESERVED_FIELDS = {"doc_id", "chunk_index", "text", "source", "title", "location", "url", "section", "container"}


def parse_date(text: str | None) -> dt.date | None:
    try:
        return dt.date.fromisoformat((text or "").strip()[:10])
    except ValueError:
        return None


def first_url(text: str | None) -> str:
    m = URL_RE.search(text or "")
    return m.group(0).rstrip(").,;") if m else ""


def norm_path(path: str) -> str:
    return path.strip().replace("\\", "/").lstrip("./").lower()


@dataclass
class ControlFolder:
    """The control folder of a documents root, with its registries merged (cached by file mtime)."""

    docs_root: Path
    path: Path
    config: dict
    _cache_key: tuple = field(default=(), repr=False)
    _rows: list[dict] = field(default_factory=list, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def load(cls, docs_root: str | Path, control_dir: str) -> "ControlFolder | None":
        docs_root = Path(docs_root)
        path = docs_root / control_dir
        if not control_dir or not path.is_dir():
            return None
        config = dict(DEFAULTS)
        config_file = path / "control_config.json"
        if config_file.is_file():
            try:
                config.update(json.loads(config_file.read_text(encoding="utf-8")))
            except (OSError, ValueError) as e:
                log.warning("Could not read %s: %s (using defaults)", config_file, e)
        return cls(docs_root=docs_root, path=path, config=config)

    @property
    def index_fields(self) -> tuple[str, ...]:
        fields = self.config.get("index_fields")
        if not isinstance(fields, list):
            return tuple(DEFAULTS["index_fields"])
        return tuple(f for f in fields if isinstance(f, str) and FIELD_NAME.match(f) and f not in RESERVED_FIELDS)

    @property
    def ocr_sidecar_suffix(self) -> str:
        return self.config.get("ocr_sidecar_suffix", "")

    def _notes(self, key: str, max_chars: int) -> str:
        name = self.config.get(key)
        if not name:
            return ""
        path = (self.path / name).resolve()
        if not path.is_relative_to(self.path.resolve()) or not path.is_file():
            log.warning("%s not found in the control folder: %s", key, name)
            return ""
        return path.read_text(encoding="utf-8", errors="replace").strip()[:max_chars]

    def assistant_notes(self, max_chars: int = 6000) -> str:
        """Instructions for the assistant from the collection's administrator (assistant_notes_file)."""
        return self._notes("assistant_notes_file", max_chars)

    def search_notes(self, max_chars: int = 3000) -> str:
        """How to read the questions of this collection and what to look for besides the topic
        (search_notes_file), for the search preparation step."""
        return self._notes("search_notes_file", max_chars)

    @property
    def excluded_dirs(self) -> set[str]:
        return {d.lower() for d in self.config["excluded_dirs"]} | {self.path.name.lower()}

    def registry_files(self) -> list[Path]:
        seen: list[Path] = []
        for pattern in self.config["registry_globs"]:
            for p in sorted(self.path.glob(pattern)):
                if p.name != self.config["master_file"] and p not in seen:
                    seen.append(p)
        return seen

    def _read(self, path: Path) -> list[dict]:
        aliases = self.config["column_aliases"]
        with open(path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f, delimiter=self.config["csv_delimiter"])
            return [{aliases.get(k.strip(), k.strip()): (v or "").strip() for k, v in row.items() if k}
                    for row in reader]

    def rows(self) -> list[dict]:
        """All registry rows, de-duplicated (by file, or by jurisdiction + reference URL when there is none)."""
        files = self.registry_files()
        key = tuple((p.name, p.stat().st_mtime, p.stat().st_size) for p in files)
        with self._lock:
            if key != self._cache_key:
                rows, keys = [], set()
                for p in files:
                    try:
                        registry_rows = self._read(p)
                    except (OSError, csv.Error, UnicodeDecodeError) as e:
                        log.warning("Could not read registry %s: %s", p.name, e)
                        continue
                    for row in registry_rows:
                        row["file"] = row.get("file", "").replace("\\", "/")
                        k = (("file", norm_path(row["file"])) if row["file"]
                             else ("none", row.get("jurisdiction", "").lower(), row.get("reference_url", "").lower()))
                        if k in keys:
                            continue
                        keys.add(k)
                        row["registry"] = p.name
                        rows.append(row)
                self._rows, self._cache_key = rows, key
            return self._rows

    def lookup(self) -> dict[str, dict]:
        """Registry row of each document, keyed by normalized relative path."""
        return {norm_path(r["file"]): r for r in self.rows() if r.get("file")}

    def documents_on_disk(self) -> list[str]:
        extensions = {e.lower() for e in self.config["document_extensions"]}
        excluded = self.excluded_dirs
        out = []
        for p in self.docs_root.rglob("*"):
            rel = p.relative_to(self.docs_root)
            if p.suffix.lower() in extensions and p.is_file() and not excluded & {x.lower() for x in rel.parts[:-1]}:
                out.append(rel.as_posix())
        return sorted(out)


def document_metadata(row: dict | None, index_fields: tuple[str, ...] = ()) -> dict:
    """The fields of a registry row that matter for retrieval and citations (plus the indexed ones)."""
    if not row:
        return {}
    meta = {k: row.get(k, "") for k in ("title", "doc_type", "number", "year", "issuer", "scope", "jurisdiction",
                                        "status", "valid_until", *index_fields)}
    meta["status"] = meta["status"].lower()
    meta["url"] = first_url(row.get("reference_url")) or first_url(row.get("download_url"))
    return {k: v for k, v in meta.items() if v}


def metadata_version(meta: dict) -> str:
    """Short hash of the metadata: a registry edit reindexes only that document."""
    return hashlib.sha256(json.dumps(meta, sort_keys=True).encode()).hexdigest()[:12] if meta else ""


# ---------------------------------------------------------------- review

def _finding(section: str, row: dict | None = None, **extra) -> dict:
    row = row or {}
    item = {"section": section, "file": row.get("file", ""), "jurisdiction": row.get("jurisdiction", ""),
            "title": row.get("title", ""), "hint": row.get("change_check", ""),
            "reference_url": first_url(row.get("reference_url"))}
    item.update(extra)
    return {k: v for k, v in item.items() if v not in ("", None)}


def check_dates(row: dict, config: dict, today: dt.date) -> dict | None:
    until = parse_date(row.get("valid_until"))
    if until and until < today:
        return _finding("expired", row, date=until.isoformat())
    if until and (until - today).days <= config["warn_days_before_expiry"]:
        return _finding("expiring", row, date=until.isoformat())
    retrieved = parse_date(row.get("retrieved_date"))
    days = config["review_periods_days"].get(row.get("review_frequency", ""), config["default_review_days"])
    if retrieved and (today - retrieved).days >= days:
        return _finding("review_due", row, date=retrieved.isoformat())
    return None


def visible_text(data: bytes) -> bytes:
    """HTML reduced to its visible text, so layout or script changes are not reported as changes."""
    text = data.decode("utf-8", errors="ignore")
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", text)
    text = html.unescape(re.sub(r"(?s)<[^>]+>", " ", text))
    return re.sub(r"\s+", " ", text).strip().encode("utf-8")


class RemoteChecker:
    """Fingerprints a URL (streamed SHA-256), using ETag / Last-Modified to skip unchanged downloads."""

    def __init__(self, user_agent: str, timeout: float, client: httpx.Client | None = None,
                 insecure_client: httpx.Client | None = None):
        headers = {"User-Agent": user_agent}
        self.client = client or httpx.Client(headers=headers, timeout=timeout, follow_redirects=True)
        self.insecure = insecure_client or httpx.Client(headers=headers, timeout=timeout, follow_redirects=True,
                                                        verify=False)  # noqa: S501 - read-only fingerprints

    def fingerprint(self, url: str, previous: dict | None) -> dict:
        headers = {}
        if previous and previous.get("etag"):
            headers["If-None-Match"] = previous["etag"]
        if previous and previous.get("last_modified"):
            headers["If-Modified-Since"] = previous["last_modified"]
        note = ""
        try:
            return self._get(self.client, url, headers, previous, note)
        except httpx.ConnectError as e:
            if "CERTIFICATE" not in str(e).upper():
                raise
        # Weak certificate on a public page: still fingerprinted read-only, and reported
        return self._get(self.insecure, url, headers, previous, "invalid TLS certificate")

    @staticmethod
    def _get(client: httpx.Client, url: str, headers: dict, previous: dict | None, note: str) -> dict:
        with client.stream("GET", url, headers=headers) as r:
            if r.status_code == 304 and previous:
                return {**previous, "not_modified": True}
            r.raise_for_status()
            content_type = r.headers.get("content-type", "").split(";")[0].strip()
            if "html" in content_type.lower():
                fingerprint = hashlib.sha256(visible_text(r.read())).hexdigest()
            else:
                digest = hashlib.sha256()
                for chunk in r.iter_bytes(1 << 20):
                    digest.update(chunk)
                fingerprint = digest.hexdigest()
            return {"fingerprint": fingerprint, "content_type": content_type, "note": note,
                    "etag": r.headers.get("etag", ""), "last_modified": r.headers.get("last-modified", "")}


def local_findings(folder: ControlFolder, rows: list[dict], today: dt.date) -> list[dict]:
    """Dates, registered files missing on disk, rows without a document and documents without a row."""
    findings: list[dict] = []
    for row in rows:
        if not row.get("file"):
            findings.append(_finding("no_document", row, notes=row.get("notes", "")))
        elif not (folder.docs_root / row["file"]).is_file():
            findings.append(_finding("missing", row))
        dated = check_dates(row, folder.config, today)
        if dated:
            findings.append(dated)
    registered = {norm_path(r["file"]) for r in rows if r.get("file")}
    findings += [{"section": "unregistered", "file": rel} for rel in folder.documents_on_disk()
                 if norm_path(rel) not in registered]
    return findings


def remote_findings(rows: list[dict], state: dict[str, dict], checker: RemoteChecker, today: dt.date,
                    workers: int) -> list[dict]:
    """Fingerprints every download URL; updates `state` in place. The first check only stores fingerprints."""
    targets = [(row, url.strip()) for row in rows
               for url in row.get("download_url", "").split("|") if url.strip().startswith("http")]

    def fingerprint(target):
        row, url = target
        try:
            return row, url, checker.fingerprint(url, state.get(url)), None
        except Exception as e:  # noqa: BLE001 - any network failure is reported
            return row, url, None, f"{type(e).__name__}: {e}"[:300]

    findings: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for row, url, result, error in pool.map(fingerprint, targets):
            if error:
                findings.append(_finding("unreachable", row, url=url, error=error))
                continue
            previous = state.get(url) or {}
            changed = (previous and not result.pop("not_modified", False)
                       and previous["fingerprint"] != result["fingerprint"])
            result.pop("not_modified", None)
            if changed:
                findings.append(_finding("changed", row, url=url, date=previous.get("checked_at", "")))
            result["changed_at"] = today.isoformat() if changed else previous.get("changed_at") or today.isoformat()
            result["checked_at"] = today.isoformat()
            state[url] = result
    return findings


def review(folder: ControlFolder, remote_state: dict[str, dict] | None = None, online: bool = True,
           checker: RemoteChecker | None = None, today: dt.date | None = None, workers: int = 6) -> dict:
    """Runs the review. Returns {records, registries, online, counts, findings, remote_state (updated)}."""
    today = today or dt.date.today()
    rows = folder.rows()
    findings = local_findings(folder, rows, today)
    state = dict(remote_state or {})
    if online:
        checker = checker or RemoteChecker(folder.config["user_agent"], folder.config["timeout_seconds"])
        findings += remote_findings(rows, state, checker, today, workers)
    counts = {s: sum(1 for f in findings if f["section"] == s) for s in SECTIONS}
    return {"records": len(rows), "registries": len(folder.registry_files()), "online": online,
            "counts": counts, "findings": findings, "remote_state": state}


def main() -> int:
    import argparse
    import sys

    from .config import get_settings
    from .db import Database

    ap = argparse.ArgumentParser(description="Review the source registries of the control folder (no AI)")
    ap.add_argument("action", choices=["check"])
    ap.add_argument("--offline", action="store_true", help="dates and local files only")
    args = ap.parse_args()
    settings = get_settings()
    folder = ControlFolder.load(settings.docs_path, settings.control_dir)
    if folder is None:
        print(f"No control folder at {settings.docs_path}/{settings.control_dir}")
        return 1
    db = Database(settings.database_url, max_size=1)
    db.migrate()
    try:
        from .control_store import ControlStore

        result = ControlStore(db).run(folder, online=not args.offline, workers=settings.control_check_workers)
        print(json.dumps(result["counts"], indent=1))
        for f in result["findings"][:200]:
            print(f"- [{f['section']}] {f.get('file') or f.get('jurisdiction')} {f.get('date', '')} {f.get('url', '')}")
        return 0
    finally:
        db.close()
        sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())
