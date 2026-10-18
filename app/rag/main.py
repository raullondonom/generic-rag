"""REST API + web UI + ingestion scheduler."""

from __future__ import annotations

import json
import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import health
from .chat import ChatService
from .config import get_settings
from .conversations import ConversationStore
from .control import ControlFolder
from .control_store import ControlStore
from .db import Database
from .embeddings import Embedder
from .feedback import REASONS, FeedbackError, FeedbackStore
from .i18n import normalize, t
from .ingest.confluence_source import ConfluenceSource
from .ingest.folder_source import FolderSource
from .ingest.pipeline import Indexer
from .ingest.state import StateStore
from .llm import build_llms
from .llm.spending import BudgetExhausted, SpendTracker
from .vectorstore import VectorStore

settings = get_settings()
logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("rag")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


class Services:
    def __init__(self):
        self.settings = settings
        self.db = Database(settings.database_url)
        self.db.migrate()
        self.state = StateStore(self.db)
        self.embedder = Embedder(settings)
        self.store = VectorStore(settings)
        self.indexer = Indexer(settings, self.embedder, self.store, self.state)
        self.folder = FolderSource(settings, self.indexer)
        self.confluence = ConfluenceSource(settings, self.indexer)
        self.spend = SpendTracker(self.db, settings.monthly_budget_usd, settings.tz, language=settings.language)
        self.conversations = ConversationStore(self.db)
        self.feedback = FeedbackStore(self.db)
        self.control = ControlStore(self.db)
        self.chat = ChatService(settings, self.embedder, self.store, build_llms(settings, self.spend),
                                self.conversations)
        self.scheduler = BackgroundScheduler(timezone="UTC")

    def prepare_index(self) -> None:
        """If the embeddings model changed: empty state and the previous model's collections go away."""
        if self.state.reset_for_collection(settings.collection_name):
            log.info("New index for %s: everything will be indexed from scratch", settings.embedding_model)
            self.store.drop_other_collections()

    def _embeddings_ready(self, source: str, retry) -> bool:
        """If the embeddings service is not up yet (it downloads the model on the first start), retry in a minute."""
        try:
            self.embedder.info()
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("Embeddings service not available yet (%s): %s retries in 1 minute", type(e).__name__, source)
            self.state.run_started(source)
            self.state.run_finished(source, "waiting", t(settings.language, "embeddings_waiting"))
            if self.scheduler.running:
                self.scheduler.add_job(retry, "date", id=f"{source}-retry", replace_existing=True,
                                       run_date=datetime.now(timezone.utc) + timedelta(minutes=1))
            return False

    def sync_folder(self, full: bool = False) -> None:
        if self._embeddings_ready("folder", lambda: self.sync_folder(full)):
            self.folder.sync(full=full)

    def sync_confluence(self, full: bool = False) -> None:
        if self._embeddings_ready("confluence", lambda: self.sync_confluence(full)):
            self.confluence.sync(full=full)

    def check_sources(self, online: bool | None = None) -> None:
        """Daily review of the source registries (no AI, nothing downloaded or reindexed)."""
        folder = ControlFolder.load(settings.docs_path, settings.control_dir)
        if folder is None:
            return
        try:
            self.control.run(folder, online=settings.control_check_online if online is None else online,
                             workers=settings.control_check_workers)
        except Exception:  # noqa: BLE001 - the error is stored with the run
            log.exception("Source control review failed")

    def purge_conversations(self) -> None:
        removed = self.conversations.purge(settings.conversation_retention_days)
        if removed:
            log.info("Old conversations deleted: %d", removed)

    def start(self) -> None:
        self.prepare_index()
        now = datetime.now(timezone.utc)
        if settings.folder_scan_interval > 0:
            self.scheduler.add_job(self.sync_folder, "interval", seconds=settings.folder_scan_interval, id="folder",
                                   next_run_time=now + timedelta(seconds=3), max_instances=1, coalesce=True)
        else:
            log.info("Automatic folder updates are off (FOLDER_SCAN_INTERVAL=0): reindex manually")
        if self.confluence.configured:
            daily = settings.confluence_daily_sync
            if daily:
                self.scheduler.add_job(self.sync_confluence, "cron", hour=daily[0], minute=daily[1],
                                       timezone=ZoneInfo(settings.tz), id="confluence",
                                       max_instances=1, coalesce=True, misfire_grace_time=3600)
                log.info("Confluence syncs every day at %s (%s)", settings.confluence_sync_time, settings.tz)
            # First start (never synced): do not wait for the daily run
            if "last_sync" not in self.state.summary().get("confluence", {}):
                self.scheduler.add_job(self.sync_confluence, "date", run_date=now + timedelta(seconds=10))
        elif settings.confluence_enabled:
            log.warning("CONFLUENCE_ENABLED=true but the URL, credentials or CONFLUENCE_SPACES are missing")
        control_daily = settings.control_daily_check
        if control_daily:
            self.scheduler.add_job(self.check_sources, "cron", hour=control_daily[0], minute=control_daily[1],
                                   timezone=ZoneInfo(settings.tz), id="control", max_instances=1, coalesce=True,
                                   misfire_grace_time=3600)
            log.info("Source control review every day at %s (%s), only when %s exists",
                     settings.control_check_time, settings.tz, settings.control_dir)
        self.scheduler.add_job(self.purge_conversations, "interval", hours=24, id="purge",
                               next_run_time=now + timedelta(minutes=1))
        self.scheduler.start()

    def stop(self) -> None:
        self.scheduler.shutdown(wait=False)
        self.db.close()


services: Services | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global services
    services = Services()
    services.start()
    if not services.chat.llms:
        log.error("No AI provider: set ANTHROPIC_API_KEY, DRY_RUN=true or OLLAMA_URL")
    log.info("RAG ready. Providers: %s (default: %s). Embeddings: %s",
             ", ".join(services.chat.llms) or "none", settings.default_provider, settings.embedding_model)
    yield
    services.stop()


app = FastAPI(title="Generic RAG", lifespan=lifespan)


# ---------------------------------------------------------------- models

class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=8000)
    conversation_id: str | None = None
    provider: str | None = None


class LookupRequest(BaseModel):
    ids: list[str] = Field(default_factory=list, max_length=500)


class FeedbackRequest(BaseModel):
    rating: int = Field(ge=-1, le=1)
    reason: str | None = None
    comment: str | None = Field(default=None, max_length=2000)


class ControlCheckRequest(BaseModel):
    online: bool = True


class ReindexRequest(BaseModel):
    source: str = "all"  # folder | confluence | all
    full: bool = False


# ---------------------------------------------------------------- routes

@app.get("/health")
def get_health():
    result = health.check(services)
    return JSONResponse(result, status_code=200 if result["ok"] else 503)


@app.post("/api/chat")
async def chat(req: ChatRequest):
    try:
        services.chat.pick_llm(req.provider)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    async def events():
        try:
            async for kind, data in services.chat.answer(req.question, req.conversation_id, req.provider):
                yield f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        except BudgetExhausted as e:
            yield f"event: error\ndata: {json.dumps(str(e), ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001
            log.exception("Error generating the answer")
            yield f"event: error\ndata: {json.dumps(f'{type(e).__name__}: {e}', ensure_ascii=False)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/conversations/lookup")
def lookup_conversations(req: LookupRequest):
    """Summary of the conversations whose ids the browser knows (there is no global listing)."""
    return services.conversations.lookup(req.ids)


@app.get("/api/conversations/{conversation_id}")
def get_conversation(conversation_id: str):
    conv = services.conversations.get(conversation_id)
    if conv is None:
        raise HTTPException(404, "Conversation not found")
    return conv


@app.delete("/api/conversations/{conversation_id}")
def delete_conversation(conversation_id: str):
    if not services.conversations.delete(conversation_id):
        raise HTTPException(404, "Conversation not found")
    return {"deleted": conversation_id}


@app.put("/api/conversations/{conversation_id}/messages/{message_id}/feedback")
def rate_message(conversation_id: str, message_id: int, req: FeedbackRequest):
    """Optional rating of an answer. Only works with the conversation id (which acts as the key)."""
    try:
        return services.feedback.set(conversation_id, message_id, req.rating, req.reason, req.comment)
    except FeedbackError as e:
        raise HTTPException(422, str(e)) from e
    except LookupError as e:
        raise HTTPException(404, str(e)) from e


@app.delete("/api/conversations/{conversation_id}/messages/{message_id}/feedback")
def unrate_message(conversation_id: str, message_id: int):
    return {"deleted": services.feedback.clear(conversation_id, message_id)}


@app.get("/api/feedback/summary")
def feedback_summary(days: int = Query(30, ge=1, le=3650)):
    """Aggregated ratings (no conversation content), to see which provider or model to improve."""
    return {**services.feedback.summary(days), "reasons": list(REASONS)}


@app.get("/api/control")
def control_status(findings: int = Query(200, ge=0, le=10000)):
    """Source control folder: registries and the last review (findings truncated to `findings`)."""
    folder = ControlFolder.load(settings.docs_path, settings.control_dir)
    last = services.control.last_run()
    if last and last.get("findings") is not None:
        last["total_findings"] = len(last["findings"])
        last["findings"] = last["findings"][:findings]
    return {
        "configured": folder is not None,
        "control_dir": settings.control_dir,
        "registries": [p.name for p in folder.registry_files()] if folder else [],
        "records": len(folder.rows()) if folder else 0,
        "check_time": settings.control_check_time,
        "running": services.control.lock.locked(),
        "last_run": last,
    }


@app.post("/api/control/check")
def control_check(req: ControlCheckRequest):
    """Starts a review in the background. It only reports: nothing is downloaded or reindexed."""
    if ControlFolder.load(settings.docs_path, settings.control_dir) is None:
        raise HTTPException(404, f"No control folder ({settings.control_dir}) in the documents folder")
    if services.control.lock.locked():
        return {"started": False, "reason": "already running"}
    threading.Thread(target=services.check_sources, kwargs={"online": req.online}, daemon=True).start()
    return {"started": True, "online": req.online}


@app.get("/api/status")
def status():
    llms = services.chat.llms
    return {
        "language": normalize(settings.language),
        "providers": list(llms),
        "default_provider": settings.default_provider if settings.default_provider in llms else next(iter(llms), None),
        "models": {name: llm.model for name, llm in llms.items()},
        "embed_model": settings.embedding_model,
        "chunks_in_index": services.store.count(),
        "sources": services.state.summary(),
        "spend": services.spend.summary() if "anthropic" in llms else None,
        "dry_run": settings.dry_run,
        "confluence": {
            "configured": services.confluence.configured,
            "url": settings.confluence_url,
            "spaces": settings.confluence_space_keys,
            "sync_time": settings.confluence_sync_time,
            "running": services.confluence.lock.locked(),
        },
        "folder": {"path": settings.docs_path, "running": services.folder.lock.locked(),
                   "auto": settings.folder_scan_interval > 0},
    }


@app.get("/api/documents")
def documents(source: str | None = None, status: str | None = None, limit: int = Query(500, le=5000)):
    return services.state.list(source=source, status=status, limit=limit)


@app.post("/api/reindex")
def reindex(req: ReindexRequest):
    started = []
    if req.source in ("folder", "all"):
        if services.folder.lock.locked():
            started.append("folder: already running")
        else:
            threading.Thread(target=services.sync_folder, kwargs={"full": req.full}, daemon=True).start()
            started.append("folder")
    if req.source in ("confluence", "all"):
        if not services.confluence.configured:
            started.append("confluence: not configured")
        elif services.confluence.lock.locked():
            started.append("confluence: already running")
        else:
            threading.Thread(target=services.sync_confluence, kwargs={"full": req.full}, daemon=True).start()
            started.append("confluence")
    if not started:
        raise HTTPException(400, "source must be folder, confluence or all")
    return {"started": started, "full": req.full}


@app.get("/api/file")
def get_file(path: str):
    """Serves a file of the documents folder (to open a cited source)."""
    root = Path(settings.docs_path).resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(target, filename=target.name, content_disposition_type="inline")


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
