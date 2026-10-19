"""REST API + web UI + ingestion scheduler."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from redis.exceptions import RedisError

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
from .ingest.contextual import Contextualizer
from .ingest.folder_source import FolderSource
from .ingest.pipeline import Indexer
from .ingest.state import StateStore
from .jobs import FINAL_EVENTS, FINAL_STATUSES, AnswerCache, JobQueue, QueueFull, Worker, cacheable_key, \
    forward_events, valid_job_id
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
INDEX_VERSION_TTL = 15  # seconds the "version" of the index (part of the answer cache key) is cached
PROVIDER_REFRESH = 10  # seconds between reads of the provider chosen in the admin panel (another process sets it)
EVENT_ID_RE = re.compile(r"^(0|\d+-\d+)$")


class Services:
    def __init__(self):
        self.settings = settings
        self.db = Database(settings.database_url, max_size=settings.db_pool_max)
        self.db.migrate()
        self.state = StateStore(self.db)
        self.embedder = Embedder(settings)
        self.store = VectorStore(settings)
        self.spend = SpendTracker(self.db, settings.monthly_budget_usd, settings.tz, language=settings.language)
        self.indexer = Indexer(settings, self.embedder, self.store, self.state,
                               contextualizer=self._contextualizer())
        self.folder = FolderSource(settings, self.indexer)
        self.confluence = ConfluenceSource(settings, self.indexer)
        self.conversations = ConversationStore(self.db)
        self.feedback = FeedbackStore(self.db)
        self.control = ControlStore(self.db)
        self.chat = ChatService(settings, self.embedder, self.store, build_llms(settings, self.spend),
                                self.conversations, notes=self.assistant_notes, describe=self.describe_collection,
                                search_notes=self.search_notes)
        self.chat.default_override = self.db.get_setting(DEFAULT_PROVIDER_KEY)
        self.scheduler = BackgroundScheduler(timezone="UTC")
        self._field_values: dict[str, tuple[float, list[str]]] = {}
        self._index_version: tuple[float, str] = (0.0, "")
        self.queue: JobQueue | None = None
        self.cache: AnswerCache | None = None
        self.worker: Worker | None = None
        self.tasks: set[asyncio.Task] = set()

    def index_version(self) -> str:
        """Changes whenever a document is indexed, updated or removed: cached answers of an older index are
        never served."""
        stamp, value = self._index_version
        if value and time.monotonic() - stamp < INDEX_VERSION_TTL:
            return value
        try:
            with self.db.conn() as c:
                row = c.execute("SELECT COUNT(*) AS n, COALESCE(MAX(updated_at)::text, '') AS last "
                                "FROM documents").fetchone()
            value = f"{settings.collection_name}:{row['n']}:{row['last']}"
        except Exception:  # noqa: BLE001 - keep the previous one rather than failing a question
            log.exception("Could not compute the index version")
            return value or settings.collection_name
        self._index_version = (time.monotonic(), value)
        return value

    def refresh_default_provider(self) -> None:
        """The admin panel runs in one process; the others read the choice from the database."""
        try:
            self.chat.default_override = self.db.get_setting(DEFAULT_PROVIDER_KEY)
        except Exception:  # noqa: BLE001 - keep the last known choice
            log.warning("Could not read the provider setting")

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
        return branding.with_options(brand.get("context_fields", []), self.field_values)

    def _contextualizer(self):
        if not settings.contextual_indexing:
            return None
        if settings.dry_run or not settings.anthropic_api_key:
            log.warning("CONTEXTUAL_INDEXING needs a Claude API key (and DRY_RUN=false): indexing without notes")
            return None
        return Contextualizer(settings, self.db, self.spend, describe=self.describe_collection,
                              guidance=self.search_notes)

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
        elif not self.store.exists() and self.state.clear_documents():
            # Qdrant lost the collection (new cluster, deleted volume): the state says "indexed" but nothing is
            log.warning("The collection %s does not exist in Qdrant: everything will be indexed again",
                        settings.collection_name)
        if settings.keyword_search:
            try:
                self.store.ensure_text_index()  # built in the background on an existing collection
            except Exception:  # noqa: BLE001 - without it the keyword searches only find less
                log.exception("Could not create the full-text index")

    def _embeddings_ready(self, source: str, retry) -> bool:
        """If the embeddings service is not up yet (it downloads the model on the first start), retry in a minute."""
        waiting = "embeddings_waiting"
        try:
            self.embedder.info()
            waiting = "cluster_waiting"
            self.store.require_peers()  # a collection with several copies needs every Qdrant node up
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("Embeddings or Qdrant not available yet (%s): %s retries in 1 minute", type(e).__name__,
                        source)
            self.state.run_started(source)
            self.state.run_finished(source, "waiting", t(settings.language, waiting))
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
        """Indexing, source review and purge run in one process only (ROLE=all or scheduler)."""
        if not settings.runs_scheduler:
            return
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

    async def start_async(self) -> None:
        """Queue, cache and worker (when REDIS_QUEUE_URL is set)."""
        if settings.redis_cache_url:
            self.cache = AnswerCache(settings.redis_cache_url, settings.cache_ttl_seconds)
        if settings.redis_queue_url:
            self.queue = JobQueue(settings.redis_queue_url, max_pending=settings.queue_max_pending,
                                  ttl=settings.job_ttl_seconds, events_ttl=settings.events_ttl_seconds,
                                  max_attempts=settings.job_max_attempts, claim_idle=settings.job_claim_idle_seconds)
            if settings.role in ("all", "worker"):
                self.worker = Worker(self.queue, self.chat, settings, self.cache, index_version=self.index_version)
                self.worker.start()
        elif settings.role == "worker":
            log.error("ROLE=worker needs REDIS_QUEUE_URL")
        self.tasks.add(asyncio.create_task(self._refresh_loop()))

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(PROVIDER_REFRESH)
            await asyncio.to_thread(self.refresh_default_provider)

    async def stop_async(self) -> None:
        for task in self.tasks:
            task.cancel()
        if self.worker:
            await self.worker.stop()
        for client in (self.queue, self.cache):
            if client:
                await client.close()

    def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        self.db.close()


services: Services | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global services
    # Every answer uses threads for the blocking clients (Qdrant, embeddings, Postgres)
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=64, thread_name_prefix="rag"))
    services = Services()
    await asyncio.to_thread(services.start)
    await services.start_async()
    if not services.chat.llms:
        log.error("No AI provider: set ANTHROPIC_API_KEY, DEEPSEEK_API_KEY, DRY_RUN=true or OLLAMA_URL")
    log.info("RAG ready. Providers: %s (default: %s). Embeddings: %s",
             ", ".join(services.chat.llms) or "none", settings.default_provider, settings.embedding_model)
    yield
    await services.stop_async()
    await asyncio.to_thread(services.stop)


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


def require_scheduler() -> None:
    """Indexing and the source review live in one process (ROLE=all or scheduler); the balancer routes there."""
    if not settings.runs_scheduler:
        raise HTTPException(409, "This instance does not run indexing (ROLE=%s): use the scheduler instance"
                            % settings.role)


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


@app.get("/health/live", include_in_schema=False)
def health_live():
    """The process is up (container restart decisions)."""
    return {"ok": True, "role": settings.role}


@app.get("/health/ready", include_in_schema=False)
async def health_ready():
    """Can take traffic: Postgres and the queue answer. The balancer and the container health check use it."""
    result = await asyncio.to_thread(health.ready, services)
    if services.queue is not None:
        try:
            await asyncio.wait_for(services.queue.redis.ping(), 3)
            result["queue"] = {"ok": True, **await services.queue.stats()}
        except (RedisError, asyncio.TimeoutError, OSError) as e:
            result.update(ok=False, queue={"ok": False, "error": type(e).__name__})
    return JSONResponse(result, status_code=200 if result["ok"] else 503)


@app.get("/api/config")
def ui_config():
    """What the end-user UI needs: the collection's branding and whether it can answer."""
    brand = branding.load(settings.docs_path, settings.control_dir, settings.language)
    fields = [{k: f[k] for k in ("id", "label", "placeholder", "options")} for f in services.context_fields(brand)]
    return {**brand, "context_fields": fields, "icon_url": "/api/branding/icon" if brand["icon"] else "/icon.svg",
            "ready": bool(services.chat.llms), "queue": services.queue is not None}


def user_context(sent: dict[str, str]) -> list[dict]:
    """Only values offered by the collection's selectors are accepted (the rest is ignored)."""
    if not sent:
        return []
    brand = branding.load(settings.docs_path, settings.control_dir, settings.language)
    return branding.resolve_context(services.context_fields(brand), sent)


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


def sse(kind: str, data, event_id: str | None = None) -> str:
    head = f"id: {event_id}\n" if event_id else ""
    return f"{head}event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def busy_response() -> JSONResponse:
    return JSONResponse({"detail": t(settings.language, "busy")}, status_code=429, headers={"Retry-After": "10"})


async def submit_job(req: ChatRequest, request: Request) -> dict:
    """Stores the question and returns its job. A question already answered (cache) is served at once without
    taking a place in the queue; the others wait their turn. Raises QueueFull when there is no room."""
    provider = req.provider if req.provider and is_admin(request) else None
    try:
        services.chat.pick_llm(provider)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    context = await asyncio.to_thread(user_context, req.context)
    queue, cache = services.queue, services.cache
    key = cacheable_key(services.chat, settings, req.question, req.conversation_id, provider, context,
                        await asyncio.to_thread(services.index_version)) if cache else None
    hit = await cache.get(key) if key else None
    job = await queue.create(req.question, req.conversation_id, context, provider, enqueue=not hit)
    if hit:
        async def serve() -> None:
            result: dict = {}
            try:
                await forward_events(queue, job["job_id"], services.chat.replay(req.question, req.conversation_id,
                                                                                hit), result)
                await queue.set_status(job["job_id"], "done")
            except Exception:  # noqa: BLE001
                log.exception("Could not serve the cached answer")
                await queue.append(job["job_id"], "error", t(settings.language, "unavailable"))
                await queue.set_status(job["job_id"], "error")

        task = asyncio.create_task(serve())
        services.tasks.add(task)
        task.add_done_callback(services.tasks.discard)
        job["cached"] = True
    return job


async def job_stream(job_id: str, after: str, request: Request | None = None):
    """SSE of a job from the event after `after` ("0" = from the start); the browser can reconnect with the last
    event id it saw and continue. Ends with `done` or `error`."""
    queue = services.queue
    last_status, announced = "", None
    while True:
        if request is not None and await request.is_disconnected():
            return
        try:
            events = await queue.read_events(job_id, after, block_ms=5000)
        except RedisError:
            log.warning("Queue not reachable while streaming %s", job_id)
            yield sse("error", t(settings.language, "unavailable"))
            return
        for event_id, kind, raw in events:
            after = event_id
            yield f"id: {event_id}\nevent: {kind}\ndata: {raw}\n\n"
            if kind in FINAL_EVENTS:
                return
        if events:
            continue
        job = await queue.get(job_id)
        if job is None:
            yield sse("error", t(settings.language, "unavailable"))
            return
        status = job["status"]
        if status == "queued":
            position = await queue.position(job)
            if (status, position) != (last_status, announced):
                yield sse("queue", {"position": position})
                last_status, announced = status, position
        if status in FINAL_STATUSES:  # finished and its events expired: the answer is in the conversation
            yield sse("gone", {"conversation_id": job.get("conversation_id") or None, "status": status})
            return
        yield ": keep-alive\n\n"


@app.post("/api/chat/jobs")
async def create_job(req: ChatRequest, request: Request):
    """Queues a question and answers 202 with the job id, which the browser keeps to recover the answer."""
    if services.queue is None:
        raise HTTPException(404, "The queue is not enabled")
    try:
        job = await submit_job(req, request)
    except QueueFull:
        return busy_response()
    except RedisError as e:
        log.exception("Queue not reachable")
        raise HTTPException(503, t(settings.language, "unavailable")) from e
    return JSONResponse(job, status_code=202)


def _job_or_404(job_id: str) -> None:
    if services.queue is None or not valid_job_id(job_id):
        raise HTTPException(404, "Job not found")


@app.get("/api/chat/jobs/{job_id}")
async def get_job(job_id: str):
    _job_or_404(job_id)
    job = await services.queue.get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    status = job["status"]
    return {"job_id": job_id, "status": status, "conversation_id": job.get("conversation_id") or None,
            "position": await services.queue.position(job) if status == "queued" else 0}


@app.get("/api/chat/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request, after: str = Query("0")):
    _job_or_404(job_id)
    if await services.queue.get(job_id) is None:
        raise HTTPException(404, "Job not found")
    last = request.headers.get("last-event-id") or after
    if not EVENT_ID_RE.match(last):
        raise HTTPException(400, "Invalid event id")
    return StreamingResponse(job_stream(job_id, last, request), media_type="text/event-stream",
                             headers=SSE_HEADERS)


@app.delete("/api/chat/jobs/{job_id}")
async def cancel_job(job_id: str):
    """Stops a question: waiting ones are dropped, running ones are cut (the partial answer is saved as incomplete)."""
    _job_or_404(job_id)
    return {"cancelled": await services.queue.cancel(job_id)}


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    """Streams the answer. With the queue enabled it goes through it too (the same protection and cache) and the
    stream ends if the connection drops; clients that want to recover the answer use /api/chat/jobs."""
    if services.queue is not None:
        try:
            job = await submit_job(req, request)
        except QueueFull:
            return busy_response()
        except RedisError as e:
            log.exception("Queue not reachable")
            raise HTTPException(503, t(settings.language, "unavailable")) from e

        async def through_queue():
            yield sse("job", {"job_id": job["job_id"]})
            async for chunk in job_stream(job["job_id"], "0", request):
                yield chunk

        return StreamingResponse(through_queue(), media_type="text/event-stream", headers=SSE_HEADERS)

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

    return StreamingResponse(events(), media_type="text/event-stream", headers=SSE_HEADERS)


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


async def queue_stats() -> dict | None:
    if services.queue is None:
        return None
    try:
        return await services.queue.stats()
    except (RedisError, OSError):
        return {"error": "unreachable"}


@app.get("/api/admin/status", dependencies=[AdminOnly])
async def admin_status():
    llms = services.chat.llms
    return {
        "language": normalize(settings.language),
        "providers": list(llms),
        "configured_provider": settings.default_provider,
        "default_provider": services.chat.current_default(),
        "models": {name: llm.model for name, llm in llms.items()},
        "embed_model": settings.embedding_model,
        "chunks_in_index": await asyncio.to_thread(services.store.count),
        "sources": await asyncio.to_thread(services.state.summary),
        "spend": await asyncio.to_thread(services.spend.summary)
        if any(llm.remote for llm in llms.values()) else None,
        "role": settings.role,
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
        "queue": await queue_stats(),
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


@app.post("/api/control/check", dependencies=[AdminOnly, Depends(require_scheduler)])
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


@app.post("/api/reindex", dependencies=[AdminOnly, Depends(require_scheduler)])
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
