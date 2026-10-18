"""REST API + web UI + ingestion scheduler."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import admin, branding, health
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
DEFAULT_PROVIDER_KEY = "default_provider"
FIELD_VALUES_TTL = 300  # seconds the values of an indexed field (selector options) are cached


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
                                self.conversations, notes=self.assistant_notes, describe=self.describe_collection,
                                search_notes=self.search_notes)
        self.chat.default_override = self.db.get_setting(DEFAULT_PROVIDER_KEY)
        self.scheduler = BackgroundScheduler(timezone="UTC")
        self._field_values: dict[str, tuple[float, list[str]]] = {}

    def field_values(self, field: str) -> list[str]:
        cached = self._field_values.get(field)
        if cached and time.monotonic() - cached[0] < FIELD_VALUES_TTL:
            return cached[1]
        try:
            values = self.store.values(field)
        except Exception:  # noqa: BLE001 - without the index the selector shows no options
            log.exception("Could not list the values of %s", field)
            return cached[1] if cached else []
        self._field_values[field] = (time.monotonic(), values)
        return values

    def context_fields(self, brand: dict) -> list[dict]:
        """The collection's selectors with their options (fixed, or the indexed values of their field)."""
        out = []
        for f in brand.get("context_fields", []):
            options = f["options"] or self.field_values(f["field"])
            excluded = {x.casefold() for x in f["exclude_options"]}
            options = [o for o in options if o.casefold() not in excluded]
            if options:
                out.append({**f, "options": options})
        return out

    def set_default_provider(self, provider: str | None) -> None:
        """Provider picked in the admin panel; it survives restarts. None = back to LLM_PROVIDER."""
        with self.db.conn() as c:
            if provider:
                c.execute("INSERT INTO settings (key, value) VALUES (%s, %s) "
                          "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (DEFAULT_PROVIDER_KEY, provider))
            else:
                c.execute("DELETE FROM settings WHERE key = %s", (DEFAULT_PROVIDER_KEY,))
        self.chat.default_override = provider

    def prepare_index(self) -> None:
        """If the embeddings model changed: empty state and the previous model's collections go away."""
        if self.state.reset_for_collection(settings.collection_name):
            log.info("New index for %s: everything will be indexed from scratch", settings.embedding_model)
            self.store.drop_other_collections()
        if settings.keyword_search:
            try:
                self.store.ensure_text_index()  # built in the background on an existing collection
            except Exception:  # noqa: BLE001 - without it the keyword searches only find less
                log.exception("Could not create the full-text index")

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

    @staticmethod
    def describe_collection() -> str:
        brand = branding.load(settings.docs_path, settings.control_dir, settings.language)
        return f"{brand['name']}: {brand['tagline']}"

    @staticmethod
    def search_notes() -> str:
        folder = ControlFolder.load(settings.docs_path, settings.control_dir)
        return folder.search_notes() if folder else ""

    @staticmethod
    def assistant_notes() -> str:
        folder = ControlFolder.load(settings.docs_path, settings.control_dir)
        return folder.assistant_notes() if folder else ""

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


def require_admin(request: Request, x_admin_token: str | None = Header(default=None)) -> None:
    """Admin API: ADMIN_TOKEN in the X-Admin-Token header, or, without a token set, this machine only."""
    if not admin.authorized(settings.admin_token, x_admin_token, request.client.host if request.client else None):
        raise HTTPException(401 if settings.admin_token else 403, "Administration access required")


def is_admin(request: Request) -> bool:
    try:
        require_admin(request, request.headers.get("x-admin-token"))
        return True
    except HTTPException:
        return False


AdminOnly = Depends(require_admin)


# ---------------------------------------------------------------- models

class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=8000)
    conversation_id: str | None = None
    context: dict[str, str] = Field(default_factory=dict, max_length=branding.MAX_FIELDS)  # selector id -> value
    provider: str | None = None  # honoured only for admin requests: the admin picks the provider


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


class ProviderRequest(BaseModel):
    provider: str | None = None  # None = back to the configured default


# ---------------------------------------------------------------- public: end-user UI

@app.get("/health")
def get_health():
    result = health.check(services)
    return JSONResponse(result, status_code=200 if result["ok"] else 503)


@app.get("/api/config")
def ui_config():
    """What the end-user UI needs: the collection's branding and whether it can answer."""
    brand = branding.load(settings.docs_path, settings.control_dir, settings.language)
    fields = [{k: f[k] for k in ("id", "label", "placeholder", "options")} for f in services.context_fields(brand)]
    return {**brand, "context_fields": fields, "icon_url": "/api/branding/icon" if brand["icon"] else "/icon.svg",
            "ready": bool(services.chat.llms)}


def user_context(sent: dict[str, str]) -> list[dict]:
    """Only values offered by the collection's selectors are accepted (the rest is ignored)."""
    if not sent:
        return []
    brand = branding.load(settings.docs_path, settings.control_dir, settings.language)
    out = []
    for f in services.context_fields(brand):
        value = (sent.get(f["id"]) or "").strip()
        if value and value in f["options"]:
            out.append({"id": f["id"], "label": f["label"], "value": value,
                        "field": f["field"] if f["boost"] else None})
    return out


@app.get("/api/branding/icon")
def branding_icon():
    icon = branding.icon_path(settings.docs_path, settings.control_dir)
    if icon is None:
        return FileResponse(STATIC_DIR / "icon.svg", media_type="image/svg+xml")
    return FileResponse(icon, media_type=branding.ICON_TYPES[icon.suffix.lower()])


@app.get("/manifest.webmanifest")
def web_manifest():
    brand = branding.load(settings.docs_path, settings.control_dir, settings.language)
    return JSONResponse(branding.manifest(brand, "/api/branding/icon" if brand["icon"] else "/icon.svg"),
                        media_type="application/manifest+json")


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    provider = req.provider if req.provider and is_admin(request) else None
    try:
        services.chat.pick_llm(provider)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    unavailable = json.dumps(t(settings.language, "unavailable"), ensure_ascii=False)
    context = await asyncio.to_thread(user_context, req.context)

    async def events():
        try:
            async for kind, data in services.chat.answer(req.question, req.conversation_id, provider, context):
                yield f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        except BudgetExhausted as e:
            log.warning("Claude budget exhausted: %s", e)
            yield f"event: error\ndata: {unavailable}\n\n"
        except Exception:  # noqa: BLE001 - details go to the log, not to the end user
            log.exception("Error generating the answer")
            yield f"event: error\ndata: {unavailable}\n\n"

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


@app.get("/api/file")
def get_file(path: str):
    """Serves a file of the documents folder (to open a cited source). Reserved "_" paths are not served."""
    root = Path(settings.docs_path).resolve()
    target = (root / path).resolve()
    rel_parts = target.relative_to(root).parts if target.is_relative_to(root) else ()
    if not rel_parts or any(p.startswith(settings.ignore_prefix_list) for p in rel_parts) or not target.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(target, filename=target.name, content_disposition_type="inline")


# ---------------------------------------------------------------- admin: panel at /admin

@app.get("/api/admin/check", dependencies=[AdminOnly])
def admin_check():
    return {"ok": True, "token_required": bool(settings.admin_token)}


@app.get("/api/admin/status", dependencies=[AdminOnly])
def admin_status():
    llms = services.chat.llms
    return {
        "language": normalize(settings.language),
        "providers": list(llms),
        "configured_provider": settings.default_provider,
        "default_provider": services.chat.current_default(),
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


@app.put("/api/admin/provider", dependencies=[AdminOnly])
def admin_provider(req: ProviderRequest):
    """Provider used for every answer (the end user cannot choose). None = back to LLM_PROVIDER."""
    if req.provider is not None and req.provider not in services.chat.llms:
        raise HTTPException(400, f"Provider '{req.provider}' is not available")
    services.set_default_provider(req.provider)
    return {"default_provider": services.chat.current_default()}


@app.get("/api/feedback/summary", dependencies=[AdminOnly])
def feedback_summary(days: int = Query(30, ge=1, le=3650)):
    """Aggregated ratings (no conversation content), to see which provider or model to improve."""
    return {**services.feedback.summary(days), "reasons": list(REASONS)}


@app.get("/api/control", dependencies=[AdminOnly])
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


@app.post("/api/control/check", dependencies=[AdminOnly])
def control_check(req: ControlCheckRequest):
    """Starts a review in the background. It only reports: nothing is downloaded or reindexed."""
    if ControlFolder.load(settings.docs_path, settings.control_dir) is None:
        raise HTTPException(404, f"No control folder ({settings.control_dir}) in the documents folder")
    if services.control.lock.locked():
        return {"started": False, "reason": "already running"}
    threading.Thread(target=services.check_sources, kwargs={"online": req.online}, daemon=True).start()
    return {"started": True, "online": req.online}


@app.get("/api/documents", dependencies=[AdminOnly])
def documents(source: str | None = None, status: str | None = None, limit: int = Query(500, le=5000)):
    return services.state.list(source=source, status=status, limit=limit)


@app.post("/api/reindex", dependencies=[AdminOnly])
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


@app.get("/admin", include_in_schema=False)
def admin_page():
    return FileResponse(STATIC_DIR / "admin.html")


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
