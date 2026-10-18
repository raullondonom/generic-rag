"""Durable queue of questions (Redis Streams), answer cache and the worker that consumes the queue.

Flow: the API stores the job and adds it to a stream (answering 202 with the job id only after Redis
confirmed it); a worker reads it through a consumer group, generates the answer and appends every event to a
per-job stream that the browser reads (and can resume) by job id. A job is acknowledged only when it is
finished, so one that a worker was running when it died is claimed by another worker after
JOB_CLAIM_IDLE_SECONDS. Delivery is at-least-once.

The queue and the cache are two Redis instances on purpose: the queue must never evict data (noeviction,
AOF), the cache is a plain LRU.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import socket
import time
import unicodedata
import uuid
from collections.abc import AsyncIterator, Callable

import redis.asyncio as aioredis
from redis.exceptions import RedisError, ResponseError

from .config import Settings
from .i18n import t
from .llm.spending import BudgetExhausted

log = logging.getLogger("rag.jobs")

STREAM = "rag:jobs"
GROUP = "workers"
SEQ_KEY = "rag:seq"
STARTED_KEY = "rag:started_seq"
FINAL_EVENTS = ("done", "error")
FINAL_STATUSES = ("done", "error", "cancelled")
EVENTS_MAXLEN = 5000
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
HEARTBEAT_SECONDS = 2.0
LOCK_SECONDS = 120


class QueueFull(RuntimeError):
    pass


def valid_job_id(job_id: str) -> bool:
    return bool(JOB_ID_RE.match(job_id or ""))


# ---------------------------------------------------------------- cache keys

_IDENTIFIER_RE = re.compile(r"\d{6,}|@|\b[a-z]{3}-?\d{3}\b|\b[a-z]{3}\d{2}[a-z]\b", re.I)  # "ley 769" has a space: not a plate


def normalize_question(question: str) -> str:
    """Same question, same text: case, accents, spaces and the punctuation around it do not matter
    (on a phone people rarely type the accents)."""
    decomposed = unicodedata.normalize("NFKD", question)
    text = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text.casefold()).strip().strip("¿?¡!.,;: ")


def has_identifier(question: str) -> bool:
    """Long numbers (ID, phone, case), an e-mail or a license plate: that question is never cached."""
    return bool(_IDENTIFIER_RE.search(question))


def cache_key(question: str, context: list[dict], language: str, model: str, index_version: str) -> str:
    """The answer depends on the question, the selectors, the model and the indexed documents: when the
    index changes the version changes and the old answers are simply never found again."""
    chosen = sorted((c.get("label", ""), c.get("value", ""), c.get("field") or "") for c in context or [])
    raw = json.dumps([normalize_question(question), chosen, language, model, index_version], ensure_ascii=False)
    return "rag:answer:" + hashlib.sha256(raw.encode()).hexdigest()


# ---------------------------------------------------------------- cache

class AnswerCache:
    """Cache of complete answers. It fails open: if Redis is down every question is simply a miss."""

    def __init__(self, url: str, ttl: int):
        self.redis = aioredis.from_url(url, decode_responses=True, socket_connect_timeout=2, socket_timeout=3,
                                       health_check_interval=30)
        self.ttl = ttl

    async def get(self, key: str) -> dict | None:
        try:
            raw = await self.redis.get(key)
        except RedisError:
            log.warning("Answer cache unavailable")
            return None
        return json.loads(raw) if raw else None

    async def set(self, key: str, result: dict) -> None:
        try:
            await self.redis.set(key, json.dumps(result, ensure_ascii=False), ex=self.ttl)
        except RedisError:
            log.warning("Answer cache unavailable")

    async def acquire(self, key: str, token: str) -> bool:
        """Single flight: only one worker generates a question that many people ask at the same time."""
        try:
            return bool(await self.redis.set(f"{key}:lock", token, nx=True, ex=LOCK_SECONDS))
        except RedisError:
            return True  # without cache there is nobody to wait for

    async def release(self, key: str, token: str) -> None:
        try:
            if await self.redis.get(f"{key}:lock") == token:
                await self.redis.delete(f"{key}:lock")
        except RedisError:
            pass

    async def locked(self, key: str) -> bool:
        try:
            return bool(await self.redis.exists(f"{key}:lock"))
        except RedisError:
            return False

    async def close(self) -> None:
        await self.redis.aclose()


# ---------------------------------------------------------------- queue

class JobQueue:
    def __init__(self, url: str, *, max_pending: int, ttl: int, events_ttl: int, max_attempts: int,
                 claim_idle: int):
        # Reads block up to 15 s, so the socket timeout is longer
        self.redis = aioredis.from_url(url, decode_responses=True, socket_connect_timeout=3, socket_timeout=30,
                                       health_check_interval=30)
        self.max_pending, self.ttl, self.events_ttl = max_pending, ttl, events_ttl
        self.max_attempts, self.claim_idle_ms = max_attempts, claim_idle * 1000

    @staticmethod
    def job_key(job_id: str) -> str:
        return f"rag:job:{job_id}"

    @staticmethod
    def events_key(job_id: str) -> str:
        return f"rag:job:{job_id}:events"

    async def setup(self) -> None:
        try:
            await self.redis.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
        except ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    async def pending(self) -> int:
        """Jobs waiting or running (a finished job leaves the stream)."""
        return int(await self.redis.xlen(STREAM))

    async def create(self, question: str, conversation_id: str | None, context: list[dict],
                     provider: str | None, *, enqueue: bool = True) -> dict:
        """Stores the job and, if `enqueue`, adds it to the queue. Raises QueueFull when it has no room."""
        if enqueue and await self.pending() >= self.max_pending:
            raise QueueFull
        job_id = uuid.uuid4().hex
        seq = await self.redis.incr(SEQ_KEY) if enqueue else 0
        pipe = self.redis.pipeline(transaction=True)
        pipe.hset(self.job_key(job_id), mapping={
            "status": "queued" if enqueue else "running", "question": question,
            "conversation_id": conversation_id or "", "context": json.dumps(context, ensure_ascii=False),
            "provider": provider or "", "attempts": 0, "seq": seq, "created": int(time.time()),
        })
        pipe.expire(self.job_key(job_id), self.ttl)
        if enqueue:
            pipe.xadd(STREAM, {"job": job_id})
        await pipe.execute()
        started = int(await self.redis.get(STARTED_KEY) or 0)
        return {"job_id": job_id, "position": max(seq - started, 1) if enqueue else 0}

    async def get(self, job_id: str) -> dict | None:
        data = await self.redis.hgetall(self.job_key(job_id))
        return data or None

    async def position(self, job: dict) -> int:
        started = int(await self.redis.get(STARTED_KEY) or 0)
        return max(int(job.get("seq", 0)) - started, 1)

    async def set_status(self, job_id: str, status: str, **extra) -> None:
        await self.redis.hset(self.job_key(job_id), mapping={"status": status, **extra})

    async def mark_started(self, seq: int) -> None:
        # Approximate on purpose: it is only used to tell the user how many are ahead
        if seq > int(await self.redis.get(STARTED_KEY) or 0):
            await self.redis.set(STARTED_KEY, seq)

    async def append(self, job_id: str, kind: str, data) -> str:
        pipe = self.redis.pipeline(transaction=False)
        pipe.xadd(self.events_key(job_id), {"e": kind, "d": json.dumps(data, ensure_ascii=False)},
                  maxlen=EVENTS_MAXLEN, approximate=True)
        pipe.expire(self.events_key(job_id), self.events_ttl)
        return (await pipe.execute())[0]

    async def read_events(self, job_id: str, after: str, block_ms: int = 0) -> list[tuple[str, str, str]]:
        """(entry id, kind, json data) of the events after `after`; blocks up to `block_ms` if there are none."""
        res = await self.redis.xread({self.events_key(job_id): after}, count=100, block=block_ms or None)
        if not res:
            return []
        return [(eid, f["e"], f["d"]) for eid, f in res[0][1]]

    async def cancel(self, job_id: str) -> bool:
        job = await self.get(job_id)
        if not job or job["status"] in FINAL_STATUSES:
            return False
        await self.redis.hset(self.job_key(job_id), "cancel", 1)
        return True

    async def cancelled(self, job_id: str) -> bool:
        return bool(await self.redis.hget(self.job_key(job_id), "cancel"))

    # -- worker side

    async def read_new(self, consumer: str, count: int, block_ms: int) -> list[tuple[str, str]]:
        res = await self.redis.xreadgroup(GROUP, consumer, {STREAM: ">"}, count=count, block=block_ms)
        return [(mid, f["job"]) for mid, f in (res[0][1] if res else [])]

    async def reclaim(self, consumer: str, count: int) -> list[tuple[str, str]]:
        """Jobs whose worker stopped confirming (it died): they go to this worker."""
        res = await self.redis.xautoclaim(STREAM, GROUP, consumer, self.claim_idle_ms, "0-0", count=count)
        return [(mid, f["job"]) for mid, f in res[1] if f]

    async def touch(self, consumer: str, message_id: str) -> None:
        """Heartbeat: resets the idle time so nobody claims a job that is being worked on."""
        await self.redis.xclaim(STREAM, GROUP, consumer, 0, [message_id], justid=True)

    async def ack(self, message_id: str) -> None:
        pipe = self.redis.pipeline(transaction=True)
        pipe.xack(STREAM, GROUP, message_id)
        pipe.xdel(STREAM, message_id)
        await pipe.execute()

    async def requeue(self, job_id: str, message_id: str) -> None:
        pipe = self.redis.pipeline(transaction=True)
        pipe.xadd(STREAM, {"job": job_id})
        pipe.xack(STREAM, GROUP, message_id)
        pipe.xdel(STREAM, message_id)
        await pipe.execute()

    async def drop_consumer(self, consumer: str) -> None:
        try:
            await self.redis.xgroup_delconsumer(STREAM, GROUP, consumer)
        except RedisError:
            pass

    async def stats(self) -> dict:
        info = await self.redis.xinfo_groups(STREAM)
        group = next((g for g in info if g["name"] == GROUP), {})
        return {"pending": await self.pending(), "in_progress": group.get("pending", 0),
                "workers": group.get("consumers", 0)}

    async def close(self) -> None:
        await self.redis.aclose()


# ---------------------------------------------------------------- worker

class Worker:
    """Consumes the queue: up to `concurrency` answers at the same time."""

    def __init__(self, queue: JobQueue, chat, settings: Settings, cache: AnswerCache | None,
                 index_version: Callable[[], str] | None = None):
        self.queue, self.chat, self.settings, self.cache = queue, chat, settings, cache
        self.index_version = index_version or (lambda: "")
        self.name = f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"
        self.concurrency = settings.worker_concurrency
        self.tasks: set[asyncio.Task] = set()
        self.stopping = False
        self._runner: asyncio.Task | None = None

    def start(self) -> None:
        self._runner = asyncio.create_task(self._run(), name="worker")

    async def stop(self, grace: float = 45.0) -> None:
        """Stops taking jobs and waits for the running ones. What does not finish is claimed by another worker."""
        self.stopping = True
        if self._runner:
            self._runner.cancel()
            await asyncio.gather(self._runner, return_exceptions=True)
        if self.tasks:
            _, pending = await asyncio.wait(self.tasks, timeout=grace)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        else:
            await self.queue.drop_consumer(self.name)

    async def _run(self) -> None:
        while True:
            try:
                await self.queue.setup()
                break
            except RedisError:
                log.warning("Queue not reachable yet, retrying")
                await asyncio.sleep(3)
        log.info("Worker %s ready (up to %d answers at once)", self.name, self.concurrency)
        while not self.stopping:
            free = self.concurrency - len(self.tasks)
            if free <= 0:
                await asyncio.sleep(0.2)
                continue
            try:
                claimed = await self.queue.reclaim(self.name, free)
                if len(claimed) < free:
                    claimed += await self.queue.read_new(self.name, free - len(claimed), 2000)
            except asyncio.CancelledError:
                raise
            except RedisError:
                log.warning("Queue not reachable, retrying")
                await asyncio.sleep(2)
                continue
            for message_id, job_id in claimed:
                task = asyncio.create_task(self._job(message_id, job_id))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
            if not claimed:
                await asyncio.sleep(0.05)  # the blocking read already waits; this only guarantees a yield

    async def _fail(self, job_id: str, message_id: str, status: str = "error") -> None:
        await self.queue.append(job_id, "error", t(self.settings.language, "unavailable"))
        await self.queue.set_status(job_id, status)
        await self.queue.ack(message_id)

    async def _job(self, message_id: str, job_id: str) -> None:
        q = self.queue
        try:
            job = await q.get(job_id)
            if job is None or job["status"] in FINAL_STATUSES:  # expired, or already finished before a crash
                await q.ack(message_id)
                return
            if job["status"] == "queued" and await q.cancelled(job_id):
                await q.append(job_id, "error", t(self.settings.language, "cancelled"))
                await q.set_status(job_id, "cancelled")
                await q.ack(message_id)
                return
            attempts = await q.redis.hincrby(q.job_key(job_id), "attempts", 1)
            if attempts > q.max_attempts:
                log.error("Job %s gave up after %d attempts", job_id, attempts - 1)
                await self._fail(job_id, message_id)
                return
            await q.set_status(job_id, "running")
            await q.mark_started(int(job.get("seq", 0)))
            if attempts > 1:
                await q.append(job_id, "reset", {})  # the browser discards what the failed attempt sent
            await self._answer(message_id, job_id, job)
        except asyncio.CancelledError:
            raise  # shutdown: the job stays pending and another worker claims it
        except Exception:  # noqa: BLE001
            log.exception("Job %s failed", job_id)
            try:
                if await self._retry_left(job_id):
                    await q.requeue(job_id, message_id)
                else:
                    await self._fail(job_id, message_id)
            except RedisError:
                log.exception("Could not requeue %s: it will be claimed again", job_id)

    async def _retry_left(self, job_id: str) -> bool:
        attempts = int(await self.queue.redis.hget(self.queue.job_key(job_id), "attempts") or 0)
        if attempts >= self.queue.max_attempts:
            return False
        await self.queue.set_status(job_id, "queued")
        await asyncio.sleep(min(2 ** attempts, 10))
        return True

    async def _cached(self, key: str | None, token: str) -> tuple[dict | None, bool]:
        """(cached answer, whether this worker holds the lock to generate it)."""
        if not (key and self.cache):
            return None, False
        hit = await self.cache.get(key)
        if hit:
            return hit, False
        if await self.cache.acquire(key, token):
            return None, True
        deadline = time.monotonic() + self.settings.cache_wait_seconds
        while time.monotonic() < deadline and await self.cache.locked(key):
            await asyncio.sleep(1)
            hit = await self.cache.get(key)
            if hit:
                return hit, False
        return await self.cache.get(key), False

    async def _answer(self, message_id: str, job_id: str, job: dict) -> None:
        q, chat, settings = self.queue, self.chat, self.settings
        question, conversation_id = job["question"], job["conversation_id"] or None
        provider, context = job["provider"] or None, json.loads(job["context"])
        version = await asyncio.to_thread(self.index_version)
        key = cacheable_key(chat, settings, question, conversation_id, provider, context, version)
        token = uuid.uuid4().hex
        hit, locked = await self._cached(key, token)
        result: dict = {}
        try:
            events = (chat.replay(question, conversation_id, hit) if hit
                      else chat.answer(question, conversation_id, provider, context))
            stream = asyncio.create_task(forward_events(q, job_id, events, result))
            was_cancelled = False
            while not stream.done():
                await asyncio.wait({stream}, timeout=HEARTBEAT_SECONDS)
                if stream.done():
                    break
                await q.touch(self.name, message_id)
                if await q.cancelled(job_id):
                    was_cancelled = True
                    stream.cancel()  # the partial answer is saved as incomplete, like a closed connection
            try:
                await stream
            except asyncio.CancelledError:
                if not was_cancelled:
                    raise
            if was_cancelled:
                await q.append(job_id, "error", t(settings.language, "cancelled"))
                await q.set_status(job_id, "cancelled")
                await q.ack(message_id)
                return
        except BudgetExhausted as e:
            log.warning("Claude budget exhausted: %s", e)
            await self._fail(job_id, message_id)
            return
        finally:
            if locked and self.cache and key:
                await self.cache.release(key, token)
        if key and self.cache and not hit and result.get("complete") and result.get("guardrail") != "greeting":
            await self.cache.set(key, {k: result[k] for k in ("answer", "sources", "provider", "model", "queries",
                                                              "keywords", "guardrail")})
        await q.set_status(job_id, "done")
        await q.ack(message_id)


TOKEN_FLUSH_SECONDS = 0.15


async def forward_events(queue: JobQueue, job_id: str, events: AsyncIterator[tuple[str, object]],
                         result: dict) -> None:
    """Appends the events of an answer to the job's stream and keeps what the cache needs.
    Tokens go out in batches (a few per second) so a long answer does not fill Redis with tiny entries."""
    result.update(answer="", sources=[], provider=None, model=None, queries=[], keywords=[], guardrail=None,
                  complete=False)
    pending, last_flush = "", time.monotonic()

    async def flush() -> None:
        nonlocal pending, last_flush
        if pending:
            await queue.append(job_id, "token", pending)
            pending = ""
        last_flush = time.monotonic()

    async for kind, data in events:
        if kind == "token":
            result["answer"] += data
            pending += data
            if time.monotonic() - last_flush >= TOKEN_FLUSH_SECONDS:
                await flush()
            continue
        await flush()
        if kind == "meta":
            result.update(provider=data.get("provider"), model=data.get("model"))
            await queue.set_status(job_id, "running", conversation_id=data.get("conversation_id", ""))
        elif kind == "sources":
            result["sources"] = data
        elif kind == "done":
            result.update(queries=data.get("queries", []), keywords=data.get("keywords", []),
                          guardrail=data.get("guardrail"), complete=True)
        await queue.append(job_id, kind, data)
    await flush()


def cacheable_key(chat, settings: Settings, question: str, conversation_id: str | None, provider: str | None,
                  context: list[dict], index_version: str) -> str | None:
    """None when this question must not be cached: it carries an identifier, or earlier turns shape the answer."""
    if has_identifier(question) or (conversation_id and settings.history_turns > 0):
        return None
    try:
        llm = chat.pick_llm(provider)
    except ValueError:
        return None
    return cache_key(question, context, settings.language, f"{llm.name}:{llm.model}", index_version)
