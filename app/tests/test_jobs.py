import asyncio
import json
from types import SimpleNamespace

import fakeredis
import pytest

from rag import jobs
from rag.config import Settings
from rag.jobs import AnswerCache, JobQueue, QueueFull, Worker, cache_key, has_identifier, normalize_question


def run(coro):
    return asyncio.run(coro)


def make_queue(server, **kw):
    q = JobQueue("redis://localhost", max_pending=kw.pop("max_pending", 100), ttl=600, events_ttl=600,
                 max_attempts=kw.pop("max_attempts", 3), claim_idle=kw.pop("claim_idle", 90))
    q.redis = fakeredis.FakeAsyncRedis(server=server, decode_responses=True)
    return q


def make_cache(server):
    c = AnswerCache("redis://localhost", 600)
    c.redis = fakeredis.FakeAsyncRedis(server=server, decode_responses=True)
    return c


class FakeChat:
    """Same interface as ChatService for the worker."""

    def __init__(self, fail_times=0, answer="Respuesta"):
        self.calls, self.fail_times, self.text = 0, fail_times, answer
        self.llm = SimpleNamespace(name="fake", model="m1")

    def pick_llm(self, provider):
        return self.llm

    async def answer(self, question, conversation_id, provider, context=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("boom")
        yield "meta", {"provider": "fake", "model": "m1", "conversation_id": "conv-1"}
        yield "sources", [{"title": "Ley 769"}]
        for part in ("Res", "puesta"):
            yield "token", part
        yield "done", {"queries": [question], "keywords": [], "message_id": 7}

    async def replay(self, question, conversation_id, cached):
        yield "meta", {"provider": "fake", "model": "m1", "conversation_id": "conv-2", "cached": True}
        yield "sources", cached["sources"]
        yield "token", cached["answer"]
        yield "done", {"queries": [], "keywords": [], "message_id": 8}


def settings(**kw):
    return Settings(language="es", worker_concurrency=4, job_max_attempts=3, cache_wait_seconds=2, **kw)


async def drain(q, job_id):
    return [(k, json.loads(d)) for _, k, d in await q.read_events(job_id, "0")]


async def run_worker(worker, q, job_id, timeout=10):
    worker.start()
    try:
        for _ in range(int(timeout * 20)):
            job = await q.get(job_id)
            if job and job["status"] in jobs.FINAL_STATUSES:
                return job
            await asyncio.sleep(0.05)
        raise AssertionError("the job did not finish")
    finally:
        await worker.stop(grace=2)


# ---------------------------------------------------------------- keys

def test_normalize_question_ignores_case_accents_spacing_and_punctuation():
    assert normalize_question("  ¿Cuál es   la MULTA por no tener licencia? ") == "cual es la multa por no tener licencia"


def test_cache_key_depends_on_question_context_model_and_index():
    base = cache_key("multa", [], "es", "a:m", "v1")
    assert cache_key("MULTA?", [], "es", "a:m", "v1") == base
    assert cache_key("multa", [], "es", "a:m", "v2") != base
    assert cache_key("multa", [], "es", "b:m", "v1") != base
    assert cache_key("multa", [{"label": "Ciudad", "value": "Cali"}], "es", "a:m", "v1") != base


@pytest.mark.parametrize("text,expected", [
    ("¿Cuál es la multa por exceso de velocidad?", False),
    ("mi cédula es 1234567890", True),
    ("el carro ABC123 tiene comparendo", True),
    ("moto ABC12D", True),
    ("escríbeme a juan@correo.com", True),
    ("artículo 131 de la ley 769", False),
])
def test_has_identifier(text, expected):
    assert has_identifier(text) is expected


# ---------------------------------------------------------------- queue

def test_job_is_stored_before_answering_and_runs_to_the_end():
    async def go():
        server = fakeredis.FakeServer()
        q = make_queue(server)
        await q.setup()
        job = await q.create("¿multa?", None, [], None)
        assert await q.pending() == 1 and job["position"] == 1
        worker = Worker(q, FakeChat(), settings(), None)
        final = await run_worker(worker, q, job["job_id"])
        events = await drain(q, job["job_id"])
        assert final["status"] == "done" and final["conversation_id"] == "conv-1"
        assert [k for k, _ in events] == ["meta", "sources", "token", "done"]
        assert "".join(d for k, d in events if k == "token") == "Respuesta"
        assert await q.pending() == 0  # acknowledged: it left the stream

    run(go())


def test_queue_full_is_rejected_instead_of_piling_up():
    async def go():
        q = make_queue(fakeredis.FakeServer(), max_pending=2)
        await q.setup()
        await q.create("a", None, [], None)
        await q.create("b", None, [], None)
        with pytest.raises(QueueFull):
            await q.create("c", None, [], None)

    run(go())


def test_job_whose_worker_died_is_claimed_by_another():
    async def go():
        q = make_queue(fakeredis.FakeServer(), claim_idle=0)
        await q.setup()
        job = await q.create("¿multa?", None, [], None)
        # The first consumer reads it and dies without acknowledging
        read = await q.read_new("dead-worker", 1, 100)
        assert read and read[0][1] == job["job_id"]
        worker = Worker(q, FakeChat(), settings(), None)
        final = await run_worker(worker, q, job["job_id"])
        assert final["status"] == "done"
        assert await q.pending() == 0

    run(go())


def test_failed_attempt_is_retried_and_the_browser_is_told_to_reset():
    async def go():
        q = make_queue(fakeredis.FakeServer())
        await q.setup()
        job = await q.create("¿multa?", None, [], None)
        chat = FakeChat(fail_times=1)
        final = await run_worker(Worker(q, chat, settings(), None), q, job["job_id"], timeout=15)
        events = await drain(q, job["job_id"])
        assert final["status"] == "done" and chat.calls == 2
        assert "reset" in [k for k, _ in events]
        assert events[-1][0] == "done"

    run(go())


def test_job_gives_up_after_max_attempts_with_a_friendly_error():
    async def go():
        q = make_queue(fakeredis.FakeServer(), max_attempts=2)
        await q.setup()
        job = await q.create("¿multa?", None, [], None)
        chat = FakeChat(fail_times=99)
        final = await run_worker(Worker(q, chat, settings(), None), q, job["job_id"], timeout=15)
        events = await drain(q, job["job_id"])
        assert final["status"] == "error" and chat.calls == 2
        assert events[-1][0] == "error" and "no está disponible" in events[-1][1]
        assert await q.pending() == 0

    run(go())


def test_cancelled_while_waiting_is_never_run():
    async def go():
        q = make_queue(fakeredis.FakeServer())
        await q.setup()
        job = await q.create("¿multa?", None, [], None)
        assert await q.cancel(job["job_id"]) is True
        chat = FakeChat()
        final = await run_worker(Worker(q, chat, settings(), None), q, job["job_id"])
        assert final["status"] == "cancelled" and chat.calls == 0

    run(go())


def test_position_counts_the_jobs_ahead():
    async def go():
        q = make_queue(fakeredis.FakeServer())
        await q.setup()
        first = await q.create("a", None, [], None)
        second = await q.create("b", None, [], None)
        assert (first["position"], second["position"]) == (1, 2)
        await q.mark_started(1)
        assert await q.position(await q.get(second["job_id"])) == 1

    run(go())


# ---------------------------------------------------------------- cache

def test_second_identical_question_is_served_from_the_cache():
    async def go():
        server = fakeredis.FakeServer()
        q, cache, chat = make_queue(server), make_cache(fakeredis.FakeServer()), FakeChat()
        await q.setup()
        worker = Worker(q, chat, settings(), cache, index_version=lambda: "v1")
        first = await q.create("¿Cuál es la multa?", None, [], None)
        await run_worker(worker, q, first["job_id"])
        second = await q.create("cual es la MULTA", None, [], None)
        worker = Worker(q, chat, settings(), cache, index_version=lambda: "v1")
        await run_worker(worker, q, second["job_id"])
        events = await drain(q, second["job_id"])
        assert chat.calls == 1  # the AI was called once
        assert events[-1][0] == "done" and "".join(d for k, d in events if k == "token") == "Respuesta"

    run(go())


def test_new_index_version_does_not_serve_old_answers():
    async def go():
        q, cache, chat = make_queue(fakeredis.FakeServer()), make_cache(fakeredis.FakeServer()), FakeChat()
        await q.setup()
        for version in ("v1", "v2"):
            job = await q.create("¿multa?", None, [], None)
            await run_worker(Worker(q, chat, settings(), cache, index_version=lambda v=version: v), q, job["job_id"])
        assert chat.calls == 2

    run(go())


def test_questions_with_identifiers_are_not_cached():
    async def go():
        q, cache, chat = make_queue(fakeredis.FakeServer()), make_cache(fakeredis.FakeServer()), FakeChat()
        await q.setup()
        for _ in range(2):
            job = await q.create("placa ABC123 comparendo", None, [], None)
            await run_worker(Worker(q, chat, settings(), cache, index_version=lambda: "v1"), q, job["job_id"])
        assert chat.calls == 2

    run(go())


def test_cache_failure_does_not_break_answers():
    async def go():
        q, chat = make_queue(fakeredis.FakeServer()), FakeChat()
        cache = AnswerCache("redis://localhost:1", 60)  # nothing listens there
        await q.setup()
        job = await q.create("¿multa?", None, [], None)
        final = await run_worker(Worker(q, chat, settings(), cache, index_version=lambda: "v1"), q, job["job_id"])
        assert final["status"] == "done"

    run(go())
