"""Source: local folder mounted in the container (incremental scan).

Names starting with IGNORE_PREFIXES (".", "_", "~$" by default) are never indexed: "_" is reserved
for metadata such as the source control folder and archived versions. When the folder has a control
folder (`_control/`), each document is enriched with its registry row: title, jurisdiction, status,
valid-until date and official URL. The relative folder path always goes into the chunk context, so
similar documents of different places (e.g. one decree per municipality) are told apart.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from pathlib import Path, PurePosixPath

from ..config import Settings
from ..control import ControlFolder, document_metadata, metadata_version, norm_path
from ..i18n import t
from .loaders import LoaderOptions, Section, UnsupportedFormat, clean_text, decode_bytes, extract
from .pipeline import Indexer

log = logging.getLogger(__name__)
SOURCE = "folder"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


MIN_OCR_CHARS = 200  # an OCR file without page markers needs at least this much text to be used
PAGE_MARKER = re.compile(r"^--- (?:p[aá]gina|page) (\d+)(?: (?:de|of) \d+)? ---[ \t]*$", re.IGNORECASE | re.MULTILINE)


def ocr_sections(text: str, language: str) -> list[Section]:
    """OCR text split by its page markers ("--- Página 3 de 10 ---"); the header before the first one is dropped."""
    # Files written on Windows end lines with CRLF: "$" would only match the very last marker
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    parts = PAGE_MARKER.split(text)
    if len(parts) == 1:
        # No page markers: the whole text, unless it is just a header with no recognized content
        body = clean_text(text)
        return [Section(body)] if len(re.sub(r"\s", "", body)) >= MIN_OCR_CHARS else []
    sections = []
    for i in range(1, len(parts) - 1, 2):
        body = clean_text(parts[i + 1])
        if body:
            sections.append(Section(body, t(language, "page", n=int(parts[i]))))
    return sections


def context_lines(rel: str, meta: dict, language: str) -> list[str]:
    """Lines added under the title of every chunk: folder path and registry metadata."""
    lines = []
    folder = str(PurePosixPath(rel).parent)
    if folder != ".":
        lines.append(f"{t(language, 'ctx_folder')}: {folder}")
    doc = " ".join(v for v in (meta.get("doc_type"), meta.get("number")) if v)
    if doc and meta.get("year"):
        doc += f" ({meta['year']})"
    fields = [
        ("ctx_jurisdiction", meta.get("jurisdiction")),
        ("ctx_issuer", meta.get("issuer")),
        ("ctx_type", doc),
        ("ctx_status", meta.get("status")),
        ("ctx_valid_until", meta.get("valid_until")),
    ]
    details = [f"{t(language, key)}: {value}" for key, value in fields if value]
    if details:
        lines.append(" · ".join(details))
    return lines


class FolderSource:
    def __init__(self, settings: Settings, indexer: Indexer):
        self.settings = settings
        self.indexer = indexer
        self.root = Path(settings.docs_path)
        self.lang = settings.language
        self.prefixes = settings.ignore_prefix_list
        self.opts = LoaderOptions(settings.ocr_enabled, settings.ocr_langs, settings.language)
        self.lock = threading.Lock()
        # (mtime, size) -> hash cache so hashes are not recomputed on every scan
        self._fingerprints: dict[str, tuple[float, int, str]] = {}

    def control(self) -> ControlFolder | None:
        return ControlFolder.load(self.root, self.settings.control_dir)

    def sidecar_suffix(self, control: ControlFolder | None) -> str:
        if control and "ocr_sidecar_suffix" in control.config:
            return control.ocr_sidecar_suffix
        return self.settings.ocr_sidecar_suffix

    def _files(self, control: ControlFolder | None = None, sidecar: str = ""):
        excluded = control.excluded_dirs if control else set()
        for path in self.root.rglob("*"):
            rel = path.relative_to(self.root)
            if any(part.startswith(self.prefixes) for part in rel.parts):
                continue
            if excluded & {p.lower() for p in rel.parts[:-1]}:
                continue
            # The OCR text of a scanned PDF is indexed together with its PDF, not on its own
            if sidecar and path.name.lower().endswith(sidecar.lower()) \
                    and path.with_name(path.name[:-len(sidecar)] + ".pdf").is_file():
                continue
            if path.is_file():
                yield path, rel.as_posix()

    def _version(self, path: Path, rel: str) -> str:
        st = path.stat()
        cached = self._fingerprints.get(rel)
        if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
            return cached[2]
        digest = sha256_file(path)
        self._fingerprints[rel] = (st.st_mtime, st.st_size, digest)
        return digest

    def _sections(self, path: Path, ocr_text: Path | None, context: list[str]) -> list[Section]:
        if ocr_text is not None:
            # Scanned PDF: its OCR text is indexed instead, and the citation still opens the PDF
            sections = ocr_sections(decode_bytes(ocr_text.read_bytes()), self.lang)
            if sections:
                context.append(t(self.lang, "ctx_ocr"))
                return sections
            log.warning("OCR text of %s has no usable text: reading the PDF instead", path.name)
        return extract(path, self.opts)

    def _process(self, path: Path, rel: str, info: dict, full: bool, sidecar: str = "",
                 index_fields: tuple[str, ...] = ()) -> int:
        """Indexes one file if it is new or changed. Returns 1 if it was (re)processed."""
        doc_id = f"{SOURCE}:{rel}"
        meta = dict(source=SOURCE, title=info.get("title") or path.name, location=rel, container=str(Path(rel).parent))
        version = "error"
        ocr_text = path.with_name(path.stem + sidecar) if sidecar and path.suffix.lower() == ".pdf" else None
        if ocr_text is not None and not ocr_text.is_file():
            ocr_text = None
        try:
            size = path.stat().st_size
            if size > self.settings.max_file_mb * 1024 * 1024 and ocr_text is None:
                version = f"size:{size}"
                if full or not self.indexer.is_current(doc_id, version):
                    self.indexer.mark(doc_id, version=version, status="unsupported",
                                      error=t(self.lang, "too_big", mb=self.settings.max_file_mb), **meta)
                return 0
            version = self._version(path, rel)
            if ocr_text is not None:
                # "ocr2": OCR parsing fixed for CRLF files; older OCR versions get reindexed automatically
                version += "+ocr2:" + self._version(ocr_text, rel + sidecar)[:16]
            if info:
                version += ":" + metadata_version(info)  # a registry edit reindexes this document only
            contextualizer = getattr(self.indexer, "contextualizer", None)
            if contextualizer is not None:
                version += contextualizer.mark()  # notes enabled or changed: indexed again
            if not full and self.indexer.is_current(doc_id, version):
                return 0
            context = context_lines(rel, info, self.lang)
            sections = self._sections(path, ocr_text, context)
            extra = {k: info[k] for k in index_fields if k in info}
            self.indexer.index(doc_id, url=info.get("url", ""), version=version, sections=sections,
                               context=context, extra=extra, **meta)
        except UnsupportedFormat as e:
            self.indexer.mark(doc_id, version=version, status="unsupported", error=str(e), **meta)
        except Exception as e:  # noqa: BLE001 - a broken file must not stop ingestion
            log.exception("Error indexing %s", rel)
            self.indexer.mark(doc_id, version="error", status="error", error=f"{type(e).__name__}: {e}", **meta)
        return 1

    def sync(self, full: bool = False) -> str:
        if not self.lock.acquire(blocking=False):
            return t(self.lang, "folder_running")
        state = self.indexer.state
        try:
            state.run_started(SOURCE)
            if not self.root.exists():
                msg = t(self.lang, "folder_missing", path=self.root)
                state.run_finished(SOURCE, "error", msg)
                return msg
            control = self.control()
            registry = control.lookup() if control else {}
            sidecar = self.sidecar_suffix(control)
            fields = control.index_fields if control else ()
            seen: set[str] = set()
            changed = 0
            for path, rel in self._files(control, sidecar):
                seen.add(f"{SOURCE}:{rel}")
                info = document_metadata(registry.get(norm_path(rel)), fields)
                changed += self._process(path, rel, info, full, sidecar, fields)

            removed = state.ids_for_source(SOURCE) - seen
            for doc_id in removed:
                self.indexer.remove(doc_id)
                self._fingerprints.pop(doc_id.split(":", 1)[1], None)
            msg = t(self.lang, "folder_done", changed=changed, removed=len(removed), seen=len(seen))
            state.run_finished(SOURCE, "ok", msg)
            if changed or removed:
                log.info("Folder: %s", msg)
            return msg
        except Exception as e:  # noqa: BLE001
            log.exception("Folder sync failed")
            state.run_finished(SOURCE, "error", str(e))
            return f"Error: {e}"
        finally:
            self.lock.release()
