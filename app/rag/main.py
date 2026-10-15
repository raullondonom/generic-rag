"""API REST + interfaz web + planificador de ingesta."""

from __future__ import annotations

import json
import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .chat import ChatService
from .config import get_settings
from .embeddings import Embedder
from .ingest.confluence_source import ConfluenceSource
from .ingest.folder_source import FolderSource
from .ingest.pipeline import Indexer
from .ingest.state import StateStore
from .llm import build_llms
from .vectorstore import VectorStore

settings = get_settings()
logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("rag")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


class Services:
    def __init__(self):
        self.state = StateStore(settings.state_dir)
        self.embedder = Embedder(settings)
        self.store = VectorStore(settings)
        self.indexer = Indexer(settings, self.embedder, self.store, self.state)
        self.folder = FolderSource(settings, self.indexer)
        self.confluence = ConfluenceSource(settings, self.indexer)
        self.chat = ChatService(settings, self.embedder, self.store, build_llms(settings))
        self.scheduler = BackgroundScheduler(timezone="UTC")

    def start(self) -> None:
        now = datetime.now(timezone.utc)
        self.scheduler.add_job(self.folder.sync, "interval", seconds=settings.folder_scan_interval, id="folder",
                               next_run_time=now + timedelta(seconds=3), max_instances=1, coalesce=True)
        if self.confluence.configured:
            if settings.confluence_sync_minutes > 0:
                self.scheduler.add_job(self.confluence.sync, "interval", minutes=settings.confluence_sync_minutes,
                                       id="confluence", next_run_time=now + timedelta(seconds=10),
                                       max_instances=1, coalesce=True)
            else:
                self.scheduler.add_job(self.confluence.sync, "date", run_date=now + timedelta(seconds=10))
        elif settings.confluence_enabled:
            log.warning("CONFLUENCE_ENABLED=true pero falta URL, credenciales o CONFLUENCE_SPACES")
        self.scheduler.start()


services: Services | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global services
    services = Services()
    services.start()
    log.info("RAG listo. Proveedores: %s (por defecto: %s)", ", ".join(services.chat.llms), settings.llm_provider)
    yield
    services.scheduler.shutdown(wait=False)


app = FastAPI(title="RAG genérico", lifespan=lifespan)


# ---------------------------------------------------------------- modelos

class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=8000)
    history: list[ChatMessage] = []
    provider: str | None = None


class ReindexRequest(BaseModel):
    source: str = "all"  # folder | confluence | all
    full: bool = False


# ---------------------------------------------------------------- rutas

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/chat")
async def chat(req: ChatRequest):
    try:
        services.chat.pick_llm(req.provider)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    async def events():
        try:
            async for kind, data in services.chat.answer(req.question, [m.model_dump() for m in req.history], req.provider):
                yield f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001
            log.exception("Error generando la respuesta")
            yield f"event: error\ndata: {json.dumps(f'{type(e).__name__}: {e}', ensure_ascii=False)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/status")
def status():
    return {
        "providers": list(services.chat.llms),
        "default_provider": settings.llm_provider,
        "models": {name: llm.model for name, llm in services.chat.llms.items()},
        "embed_model": settings.embed_model,
        "chunks_in_index": services.store.count(),
        "sources": services.state.summary(),
        "confluence": {
            "configured": services.confluence.configured,
            "url": settings.confluence_url,
            "spaces": settings.confluence_space_keys,
            "running": services.confluence.lock.locked(),
        },
        "folder": {"path": settings.docs_path, "running": services.folder.lock.locked()},
    }


@app.get("/api/documents")
def documents(source: str | None = None, status: str | None = None, limit: int = Query(500, le=5000)):
    return services.state.list(source=source, status=status, limit=limit)


@app.post("/api/reindex")
def reindex(req: ReindexRequest):
    started = []
    if req.source in ("folder", "all"):
        if services.folder.lock.locked():
            started.append("carpeta: ya en curso")
        else:
            threading.Thread(target=services.folder.sync, kwargs={"full": req.full}, daemon=True).start()
            started.append("carpeta")
    if req.source in ("confluence", "all"):
        if not services.confluence.configured:
            started.append("confluence: no configurado")
        elif services.confluence.lock.locked():
            started.append("confluence: ya en curso")
        else:
            threading.Thread(target=services.confluence.sync, kwargs={"full": req.full}, daemon=True).start()
            started.append("confluence")
    if not started:
        raise HTTPException(400, "source debe ser folder, confluence o all")
    return {"started": started, "full": req.full}


@app.get("/api/file")
def get_file(path: str):
    """Descarga un archivo de la carpeta de documentos (para abrir la fuente citada)."""
    root = Path(settings.docs_path).resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(404, "Archivo no encontrado")
    return FileResponse(target, filename=target.name, content_disposition_type="inline")


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
