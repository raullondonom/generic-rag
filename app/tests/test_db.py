"""Tests against Postgres (skipped without TEST_DATABASE_URL)."""

import pytest

from rag.conversations import ConversationStore
from rag.feedback import FeedbackError, FeedbackStore
from rag.ingest.state import StateStore
from rag.llm.spending import BudgetExhausted, SpendTracker, Usage


def test_state_upsert_summary_and_reset(db):
    state = StateStore(db)
    assert state.reset_for_collection("documents__model-a") is True
    state.upsert("folder:a.md", source="folder", title="a.md", location="a.md", version="v1",
                 status="indexed", chunks=3, redactions=1)
    state.run_started("folder")
    state.run_finished("folder", "ok", "1 file")
    summary = state.summary()["folder"]
    assert summary["docs"] == {"indexed": 1}
    assert summary["chunks"] == 3
    assert summary["redactions"] == 1
    assert summary["last_sync"]["status"] == "ok"

    # Same collection: untouched. Another collection (another model): emptied to reindex
    assert state.reset_for_collection("documents__model-a") is False
    assert state.get("folder:a.md") is not None
    assert state.reset_for_collection("documents__model-b") is True
    assert state.get("folder:a.md") is None
    assert state.summary() == {}


def test_conversation_lifecycle(db):
    convs = ConversationStore(db)
    cid = convs.create("How is it deployed?")
    convs.add_message(cid, "user", "How is it deployed?")
    convs.add_message(cid, "assistant", "With the pipeline [1]", sources=[{"n": 1, "title": "a.md"}],
                      provider="ollama", model="m")
    convs.add_message(cid, "user", "and in prod?")
    convs.add_message(cid, "assistant", "partial", error="RuntimeError: x")

    conv = convs.get(cid)
    assert conv["title"] == "How is it deployed?"
    assert [m["role"] for m in conv["messages"]] == ["user", "assistant", "user", "assistant"]
    assert conv["messages"][1]["sources"] == [{"n": 1, "title": "a.md"}]
    # The history for the AI leaves out the answer with an error
    assert [m["content"] for m in convs.history(cid, 10)] == ["How is it deployed?", "With the pipeline [1]", "and in prod?"]
    assert [m["content"] for m in convs.history(cid, 2)] == ["With the pipeline [1]", "and in prod?"]

    other = convs.create("other")
    found = convs.lookup([cid, "not-a-uuid", "00000000-0000-0000-0000-000000000000"])
    assert [c["id"] for c in found] == [cid]
    assert found[0]["messages"] == 4
    assert {c["id"] for c in convs.lookup([cid, other])} == {cid, other}

    assert convs.delete(cid) is True
    assert convs.get(cid) is None
    assert convs.delete(cid) is False
    assert convs.get("garbage") is None


def test_feedback_is_optional_validated_and_survives_deletion(db):
    convs = ConversationStore(db)
    feedback = FeedbackStore(db)
    cid = convs.create("q")
    user_id = convs.add_message(cid, "user", "How is it deployed?")
    answer_id = convs.add_message(cid, "assistant", "With the pipeline [1]", sources=[{"n": 1, "title": "a.md"}],
                                  provider="ollama", model="m")
    other = convs.create("other")

    assert feedback.summary()["satisfaction"] is None
    with pytest.raises(FeedbackError):
        feedback.set(cid, answer_id, 0)
    with pytest.raises(FeedbackError):
        feedback.set(cid, answer_id, -1, reason="made-up")
    with pytest.raises(LookupError):
        feedback.set(cid, user_id, 1)  # only answers can be rated
    with pytest.raises(LookupError):
        feedback.set(other, answer_id, 1)  # the conversation id acts as the key

    feedback.set(cid, answer_id, 1, reason="wrong")
    assert convs.get(cid)["messages"][1]["rating"] == 1
    assert convs.get(cid)["messages"][1]["reason"] is None  # a reason only applies to thumbs down
    feedback.set(cid, answer_id, -1, reason="incomplete", comment="  missing the rollback  ")
    msg = convs.get(cid)["messages"][1]
    assert (msg["rating"], msg["reason"], msg["comment"]) == (-1, "incomplete", "missing the rollback")

    summary = feedback.summary()
    assert (summary["up"], summary["down"]) == (0, 1)
    assert summary["by_reason"] == {"incomplete": 1}
    assert summary["by_model"] == [{"provider": "ollama", "model": "m", "up": 0, "down": 1}]

    # Deleting the conversation keeps the feedback (with a copy of the question and answer)
    convs.delete(cid)
    rows = feedback.export(negative_only=True)
    assert rows[0]["question"] == "How is it deployed?"
    assert rows[0]["answer"] == "With the pipeline [1]"

    cid2 = convs.create("q2")
    convs.add_message(cid2, "user", "x")
    a2 = convs.add_message(cid2, "assistant", "y")
    feedback.set(cid2, a2, 1)
    assert feedback.clear(cid2, a2) is True
    assert feedback.clear(cid2, a2) is False


def test_control_review_is_stored(db, tmp_path):
    import httpx

    from rag.control import ControlFolder, RemoteChecker
    from rag.control_store import ControlStore

    (tmp_path / "_control").mkdir()
    (tmp_path / "_control" / "sources_a.csv").write_text(
        "file;title;download_url;valid_until\na.pdf;A;https://x.gov/a.pdf;2020-01-01\n", encoding="utf-8")
    (tmp_path / "a.pdf").write_bytes(b"%PDF")
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"pdf")))
    store = ControlStore(db)
    assert store.last_run() is None

    result = store.run(ControlFolder.load(tmp_path, "_control"), online=True,
                       checker=RemoteChecker("t", 5, client=client, insecure_client=client))
    assert result["counts"]["expired"] == 1
    last = store.last_run()
    assert last["status"] == "ok"
    assert last["records"] == 1
    assert last["counts"]["expired"] == 1
    assert last["findings"][0]["file"] == "a.pdf"
    assert "https://x.gov/a.pdf" in store.remote_state()

    offline = store.run(ControlFolder.load(tmp_path, "_control"), online=False)
    assert offline["counts"]["expired"] == 1
    assert store.last_run()["online"] is False
    assert "https://x.gov/a.pdf" in store.remote_state()  # an offline run keeps the fingerprints


def test_purge_old_conversations(db):
    convs = ConversationStore(db)
    old = convs.create("old")
    new = convs.create("new")
    with db.conn() as c:
        c.execute("UPDATE conversations SET updated_at = now() - interval '200 days' WHERE id = %s", (old,))
    assert convs.purge(0) == 0
    assert convs.purge(180) == 1
    assert convs.exists(new)
    assert not convs.exists(old)


def test_budget_guard(db):
    spend = SpendTracker(db, cap_usd=1.0)
    spend.check("claude-opus-5-5", 3000, 1000, "test")  # ~USD 0.024: fits
    spend.record_usage("claude-opus-5-5", "answer", Usage(output_tokens=49_000), 0.98, False)
    spend.record_usage("claude-opus-5-5", "answer", Usage(output_tokens=1000), 0.0, True)
    assert spend.month_total() == pytest.approx(0.98)
    with pytest.raises(BudgetExhausted):
        spend.check("claude-opus-5-5", 3000, 1000, "test")
    summary = spend.summary()
    assert summary["calls"] == 1  # simulated calls do not count
    assert summary["remaining_usd"] == pytest.approx(0.02)


def test_migrate_is_applied_once_and_safe_in_parallel(db):
    import threading

    from rag.db import SCHEMA_KEY

    first = db.get_setting(SCHEMA_KEY)
    assert first  # recorded by the fixture's migrate()
    errors = []

    def again():
        try:
            db.migrate()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=again) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert db.get_setting(SCHEMA_KEY) == first
