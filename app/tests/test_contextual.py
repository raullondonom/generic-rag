from types import SimpleNamespace

from rag.config import Settings
from rag.ingest.contextual import VERSION_MARK, Contextualizer, strip_mark
from rag.ingest.loaders import Section
from rag.ingest.pipeline import Indexer
from rag.llm.spending import BudgetExhausted


class FakeSpend:
    def __init__(self, budget_calls=None):
        self.budget_calls = budget_calls
        self.records = []

    def check(self, model, chars, max_tokens, purpose):
        if self.budget_calls is not None and len(self.records) >= self.budget_calls:
            raise BudgetExhausted("cap reached")

    def record(self, message, purpose, simulated=False):
        self.records.append((message.model, purpose))
        return 0.0


class FakeClient:
    def __init__(self):
        self.calls = []

        def create(**kw):
            self.calls.append(kw)
            chunk = kw["messages"][0]["content"].rsplit("\n", 1)[-1]
            return SimpleNamespace(model=kw["model"], stop_reason="end_turn",
                                   content=[SimpleNamespace(type="text", text=f"  note about\n {chunk} ")])

        self.messages = SimpleNamespace(create=create)


def make(budget_calls=None, guidance="Users look for deadlines."):
    settings = Settings(_env_file=None, anthropic_api_key="x", context_workers=2)
    client, spend = FakeClient(), FakeSpend(budget_calls)
    ctx = Contextualizer(settings, db=None, spend=spend, client=client, describe=lambda: "Handbook: policies",
                         guidance=lambda: guidance)
    cache: dict[str, str] = {}
    ctx._load = lambda keys: {k: cache[k] for k in keys if k in cache}
    ctx._save = lambda notes: cache.update(notes)
    return ctx, client, spend, cache


ITEMS = [("p. 1", "", "refund form"), ("p. 1", "refund form", "annual leave")]


def test_notes_are_written_with_the_collection_purpose_and_cached():
    ctx, client, spend, cache = make()
    assert ctx.notes("Handbook", "\nFolder: hr", ITEMS) == ["note about refund form", "note about annual leave"]
    assert len(client.calls) == 2 and len(cache) == 2
    call = client.calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert "Users look for deadlines." in call["system"][0]["text"]
    assert "Handbook: policies" in call["system"][0]["text"]
    assert spend.records == [("claude-haiku-5-5", "context")] * 2
    # Second time nothing is paid for
    assert ctx.notes("Handbook", "\nFolder: hr", ITEMS) == ["note about refund form", "note about annual leave"]
    assert len(client.calls) == 2


def test_budget_exhausted_leaves_the_rest_without_notes():
    ctx, client, _, _ = make(budget_calls=1)
    ctx.workers = 1
    assert ctx.notes("Handbook", "", ITEMS) == ["note about refund form", None]
    assert len(client.calls) == 1


def test_the_version_mark_follows_the_collection_texts():
    a, _, _, _ = make(guidance="one")
    b, _, _, _ = make(guidance="two")
    assert a.mark().startswith(VERSION_MARK) and a.mark() != b.mark()
    assert strip_mark("abc" + a.mark()) == "abc"


class FakeEmbedder:
    def __init__(self):
        self.texts = []

    def embed_documents(self, texts):
        self.texts = texts
        return [[0.0] for _ in texts]


class FakeStore:
    def delete_doc(self, doc_id):
        pass

    def upsert(self, doc_id, chunks, vectors, payloads):
        self.chunks, self.payloads = chunks, payloads


class FakeState:
    def upsert(self, doc_id, **kw):
        self.saved = kw


class FakeContextualizer:
    def __init__(self, notes):
        self._notes = notes

    def notes(self, title, context, items):
        self.items = items
        return self._notes[:len(items)]


def index(notes):
    settings = Settings(_env_file=None, chunk_size=200, chunk_overlap=0, redact_secrets=False)
    embedder, store, state = FakeEmbedder(), FakeStore(), FakeState()
    ctx = FakeContextualizer(notes)
    indexer = Indexer(settings, embedder, store, state, contextualizer=ctx)
    indexer.index("folder:a.md", source="folder", title="a.md", location="a.md", url="", version="v1+ctx:abc",
                  sections=[Section("First paragraph. " * 8 + "\n\n" + "Second paragraph. " * 8)])
    return embedder, store, state, ctx


def test_notes_are_embedded_but_the_answer_reads_the_chunk():
    embedder, store, state, ctx = index(["note one", "note two"])
    assert len(store.chunks) == 2
    assert embedder.texts[0].startswith("note one\n\n") and "note one" not in store.chunks[0]
    assert store.payloads[1]["search_note"] == "note two"
    assert ctx.items[1][1]  # the second chunk knows the end of the first one
    assert state.saved["version"] == "v1+ctx:abc"


def test_a_document_missing_notes_is_retried_on_the_next_sync():
    embedder, store, state, _ = index(["note one", None])
    assert "search_note" not in store.payloads[1]
    assert embedder.texts[1] == store.chunks[1]
    assert state.saved["version"] == "v1"
