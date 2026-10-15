"""Fuente: Confluence Cloud (solo lectura). Indexa páginas y adjuntos de los espacios configurados."""

from __future__ import annotations

import logging
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from ..config import Settings
from .loaders import LOADERS, SKIP_EXTENSIONS, LoaderOptions, Section, UnsupportedFormat, extract, html_to_text
from .pipeline import Indexer

log = logging.getLogger(__name__)
SOURCE = "confluence"
IMAGE_EXTENSIONS = {ext for ext, fn in LOADERS.items() if fn.__name__ == "load_image"}


class ConfluenceSource:
    def __init__(self, settings: Settings, indexer: Indexer):
        self.settings = settings
        self.indexer = indexer
        self.base = settings.confluence_url.rstrip("/")            # https://x.atlassian.net/wiki
        parts = urlsplit(self.base)
        self.site = f"{parts.scheme}://{parts.netloc}"               # https://x.atlassian.net
        self.opts = LoaderOptions(ocr_enabled=settings.ocr_enabled, ocr_langs=settings.ocr_langs)
        self.lock = threading.Lock()
        self._client: httpx.Client | None = None

    @property
    def configured(self) -> bool:
        s = self.settings
        return bool(s.confluence_enabled and s.confluence_url and s.confluence_email
                    and s.confluence_token and s.confluence_space_keys)

    # ------------------------------------------------------------ HTTP (solo GET)

    def _get(self, url: str, **params) -> httpx.Response:
        if not url.startswith("http"):
            url = (self.site if url.startswith("/wiki/") else self.base) + url
        for attempt in range(6):
            resp = self._client.get(url, params=params or None)
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = float(resp.headers.get("Retry-After", 2 ** attempt))
                log.warning("Confluence %s en %s, reintento en %.0fs", resp.status_code, url, wait)
                time.sleep(min(wait, 60))
                continue
            resp.raise_for_status()
            return resp
        resp.raise_for_status()
        return resp

    def _paginate(self, path: str, **params):
        url, query = path, {**params, "limit": 250}
        while url:
            data = self._get(url, **query).json()
            yield from data.get("results", [])
            url = data.get("_links", {}).get("next")
            query = {}  # el enlace "next" ya trae el cursor y los parámetros

    # ------------------------------------------------------------ sincronización

    def sync(self, full: bool = False) -> str:
        if not self.configured:
            return "Confluence no está configurado"
        if not self.lock.acquire(blocking=False):
            return "Confluence ya se está sincronizando"
        state = self.indexer.state
        state.run_started(SOURCE)
        self._client = httpx.Client(
            auth=(self.settings.confluence_email, self.settings.confluence_token),
            headers={"Accept": "application/json"},
            timeout=httpx.Timeout(120.0, connect=15.0),
            follow_redirects=True,
        )
        totals = {"pages": 0, "attachments": 0, "changed": 0, "removed": 0, "errors": 0}
        try:
            spaces = {s["key"]: s for s in self._get("/api/v2/spaces", keys=",".join(self.settings.confluence_space_keys),
                                                     limit=250).json().get("results", [])}
            missing = [k for k in self.settings.confluence_space_keys if k not in spaces]
            if missing:
                log.warning("Espacios de Confluence no encontrados o sin permiso: %s", ", ".join(missing))
            for key, space in spaces.items():
                self._sync_space(key, space, full, totals)
            msg = (f"{totals['pages']} páginas, {totals['attachments']} adjuntos, {totals['changed']} procesados, "
                   f"{totals['removed']} eliminados, {totals['errors']} errores")
            if missing:
                msg += f" · espacios no encontrados: {', '.join(missing)}"
            state.run_finished(SOURCE, "ok", msg)
            log.info("Confluence: %s", msg)
            return msg
        except Exception as e:  # noqa: BLE001
            log.exception("Error sincronizando Confluence")
            state.run_finished(SOURCE, "error", f"{type(e).__name__}: {e}")
            return f"Error: {e}"
        finally:
            self._client.close()
            self.lock.release()

    def _sync_space(self, key: str, space: dict, full: bool, totals: dict) -> None:
        log.info("Sincronizando espacio %s (%s)", key, space.get("name"))
        seen: set[str] = set()
        for page in self._paginate(f"/api/v2/spaces/{space['id']}/pages", status="current"):
            totals["pages"] += 1
            doc_id = f"{SOURCE}:page:{page['id']}"
            seen.add(doc_id)
            version = str(page.get("version", {}).get("number", ""))
            title = page.get("title", "")
            url = self.base + page.get("_links", {}).get("webui", f"/pages/viewpage.action?pageId={page['id']}")
            if full or not self.indexer.is_current(doc_id, version):
                totals["changed"] += 1
                try:
                    body = self._get(f"/api/v2/pages/{page['id']}", **{"body-format": "view"}).json()
                    html = body.get("body", {}).get("view", {}).get("value", "")
                    text = html_to_text(html) if html else ""
                    sections = [Section(f"Espacio: {space.get('name')} ({key})\n\n{text}")] if text else []
                    self.indexer.index(doc_id, source=SOURCE, title=title, location=f"{key} / {title}", url=url,
                                       version=version, sections=sections, container=key)
                except Exception as e:  # noqa: BLE001
                    totals["errors"] += 1
                    log.exception("Error en la página %s", title)
                    self.indexer.mark(doc_id, source=SOURCE, title=title, location=f"{key} / {title}",
                                      version="error", status="error", error=f"{type(e).__name__}: {e}", container=key)
            if self.settings.confluence_include_attachments:
                self._sync_attachments(key, page, title, url, full, totals, seen)

        removed = self.indexer.state.ids_for_source(SOURCE, container=key) - seen
        for doc_id in removed:
            self.indexer.remove(doc_id)
        totals["removed"] += len(removed)

    def _sync_attachments(self, key: str, page: dict, page_title: str, page_url: str, full: bool,
                          totals: dict, seen: set[str]) -> None:
        try:
            attachments = list(self._paginate(f"/api/v2/pages/{page['id']}/attachments"))
        except Exception:  # noqa: BLE001
            log.exception("No se pudieron listar los adjuntos de %s", page_title)
            totals["errors"] += 1
            # Conservar los adjuntos ya indexados de esta página
            for doc in self.indexer.state.list(source=SOURCE, limit=100000):
                if doc["doc_id"].startswith(f"{SOURCE}:att:") and doc["location"].startswith(f"{key} / {page_title} / "):
                    seen.add(doc["doc_id"])
            return
        max_bytes = self.settings.confluence_max_attachment_mb * 1024 * 1024
        for att in attachments:
            name = att.get("title", "")
            ext = Path(name).suffix.lower()
            if ext in SKIP_EXTENSIONS or (ext in IMAGE_EXTENSIONS and not self.settings.ocr_enabled):
                continue  # imágenes (capturas) sin OCR y binarios: no aportan texto
            if (att.get("fileSize") or 0) > max_bytes:
                continue
            totals["attachments"] += 1
            doc_id = f"{SOURCE}:att:{att['id']}"
            seen.add(doc_id)
            version = str(att.get("version", {}).get("number", ""))
            if not full and self.indexer.is_current(doc_id, version):
                continue
            totals["changed"] += 1
            location = f"{key} / {page_title} / {name}"
            meta = dict(source=SOURCE, title=name, location=location, container=key)
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / f"adjunto{ext}"
                    path.write_bytes(self._get(self.base + att["downloadLink"]).content)
                    sections = extract(path, self.opts)
                self.indexer.index(doc_id, url=page_url, version=version, sections=sections, **meta)
            except UnsupportedFormat as e:
                self.indexer.mark(doc_id, version=version, status="unsupported", error=str(e), **meta)
            except Exception as e:  # noqa: BLE001
                totals["errors"] += 1
                log.exception("Error en el adjunto %s", name)
                self.indexer.mark(doc_id, version="error", status="error", error=f"{type(e).__name__}: {e}", **meta)
