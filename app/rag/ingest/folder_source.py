"""Source: local folder mounted in the container (periodic incremental scan)."""

from __future__ import annotations

import hashlib
import logging
import threading
from pathlib import Path

from ..config import Settings
from ..i18n import t
from .loaders import LoaderOptions, UnsupportedFormat, extract
from .pipeline import Indexer

log = logging.getLogger(__name__)
SOURCE = "folder"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


class FolderSource:
    def __init__(self, settings: Settings, indexer: Indexer):
        self.settings = settings
        self.indexer = indexer
        self.root = Path(settings.docs_path)
        self.lang = settings.language
        self.opts = LoaderOptions(settings.ocr_enabled, settings.ocr_langs, settings.language)
        self.lock = threading.Lock()
        # (mtime, size) -> hash cache so hashes are not recomputed on every scan
        self._fingerprints: dict[str, tuple[float, int, str]] = {}

    def _files(self):
        for path in self.root.rglob("*"):
            rel = path.relative_to(self.root)
            if any(part.startswith(".") or part.startswith("~$") for part in rel.parts):
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
            seen: set[str] = set()
            changed = 0
            max_bytes = self.settings.max_file_mb * 1024 * 1024
            for path, rel in self._files():
                doc_id = f"{SOURCE}:{rel}"
                seen.add(doc_id)
                meta = dict(source=SOURCE, title=path.name, location=rel, container=str(Path(rel).parent))
                try:
                    if path.stat().st_size > max_bytes:
                        version = f"size:{path.stat().st_size}"
                        if full or not self.indexer.is_current(doc_id, version):
                            self.indexer.mark(doc_id, version=version, status="unsupported",
                                              error=t(self.lang, "too_big", mb=self.settings.max_file_mb), **meta)
                        continue
                    version = self._version(path, rel)
                    if not full and self.indexer.is_current(doc_id, version):
                        continue
                    changed += 1
                    sections = extract(path, self.opts)
                    self.indexer.index(doc_id, url="", version=version, sections=sections, **meta)
                except UnsupportedFormat as e:
                    self.indexer.mark(doc_id, version=version, status="unsupported", error=str(e), **meta)
                except Exception as e:  # noqa: BLE001 - a broken file must not stop ingestion
                    log.exception("Error indexing %s", rel)
                    self.indexer.mark(doc_id, version="error", status="error", error=f"{type(e).__name__}: {e}", **meta)

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
