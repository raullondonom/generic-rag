import asyncio

from rag.chat import ChatService
from rag.config import Settings
from rag.i18n import t
from rag.vectorstore import Hit


class FakeEmbedder:
    def __init__(self):
        self.queries = []

    def embed_query(self, text):
        return [0.0]

    def embed_queries(self, texts):
        self.queries.append(list(texts))
        return [[0.0] for _ in texts]


class FakeStore:
    def __init__(self, hits):
        self.hits = hits
        self.wheres = []

    def search(self, vector, limit, exclude_statuses=None, where=None):
        self.excluded = exclude_statuses
        self.wheres.append(where)
        if where:
            return [h for h in self.hits if all(h.payload.get(k) == v for k, v in where.items())]
        return self.hits


class FakeConversations:
    """Same interface as ConversationStore, in memory."""

    def __init__(self):
        self.data: dict[str, list[dict]] = {}

    def create(self, title):
        cid = f"c{len(self.data) + 1}"
        self.data[cid] = []
        return cid

    def exists(self, cid):
        return cid in self.data

    def history(self, cid, limit):
        return [{"role": m["role"], "content": m["content"]} for m in self.data[cid] if not m["error"]][-limit:]

    def add_message(self, cid, role, content, *, sources=None, provider=None, model=None, error=None):
        self.data[cid].append({"role": role, "content": content, "sources": sources, "error": error})
        return sum(len(msgs) for msgs in self.data.values())


class FakeLLM:
    name = "fake"
    model = "fake-model"

    def __init__(self, remote=False, fail=False, prepared=None):
        self.remote = remote
        self.fail = fail
        self.prepared = prepared
        self.calls = []
        self.rewrites = []

    async def stream(self, system, messages):
        self.calls.append((system, messages))
        yield "answer "
        if self.fail:
            raise RuntimeError("cut off")
        yield "[1]"

    async def complete_json(self, system, messages, schema):
        self.rewrites.append(messages)
        if isinstance(self.prepared, Exception):
            raise self.prepared
        return self.prepared if self.prepared is not None else {
            "corrected": "", "standalone": "rewritten query", "queries": ["other wording"], "missing": []}


def run(service, question, conversation_id=None, context=None):
    async def collect():
        return [e async for e in service.answer(question, conversation_id, "fake", context)]
    return asyncio.run(collect())


def make(hits, language="en", settings_kwargs=None, **llm_kwargs):
    llm = FakeLLM(**llm_kwargs)
    convs = FakeConversations()
    settings = Settings(_env_file=None, min_score=0.5, language=language, **(settings_kwargs or {}))
    service = ChatService(settings, FakeEmbedder(), FakeStore(hits), {"fake": llm}, convs)
    return service, llm, convs


def hit(score, doc_id="folder:a.md"):
    return Hit(score=score, text="content", payload={"doc_id": doc_id, "title": "a.md", "source": "folder"})


def meta(events):
    return next(d for k, d in events if k == "meta")


def sent_messages(llm, call=-1):
    return llm.calls[call][1]


def test_guardrail_without_relevant_context_does_not_call_llm():
    service, llm, convs = make([hit(0.2)])
    events = run(service, "What is the capital of France?")
    tokens = "".join(d for k, d in events if k == "token")
    assert tokens == t("en", "no_context")
    assert llm.calls == []
    # The guardrail answer is saved too
    assert [m["role"] for m in convs.data[meta(events)["conversation_id"]]] == ["user", "assistant"]


def test_answer_with_context_calls_llm_with_documents():
    service, llm, _ = make([hit(0.8), hit(0.7, "folder:b.md")])
    events = run(service, "How is it deployed?")
    sources = next(d for k, d in events if k == "sources")
    assert [s["n"] for s in sources] == [1, 2]
    assert len(llm.calls) == 1
    assert "<documents>" in sent_messages(llm)[-1]["content"]


def test_prompts_follow_the_language():
    service, llm, _ = make([hit(0.9)], language="es")
    events = run(service, "¿Cómo se despliega?")
    system, messages = llm.calls[0]
    assert system.startswith("Eres un asistente")
    assert "DOCUMENTOS:" in messages[-1]["content"] and "Pregunta del usuario" in messages[-1]["content"]
    assert next(d for k, d in events if k == "sources")[0]["source_label"] == "Carpeta local"


def test_collection_notes_are_added_to_the_system_prompt():
    service, llm, _ = make([hit(0.9)])
    service.notes = lambda: "- The newest version of a policy prevails."
    run(service, "How is it deployed?")
    system = llm.calls[0][0]
    assert system.endswith("- The newest version of a policy prevails.")
    assert t("en", "corpus_notes") in system
    service.notes = lambda: ""
    run(service, "How is it deployed?")
    assert t("en", "corpus_notes") not in llm.calls[1][0]


def test_greeting_short_circuit():
    service, llm, _ = make([hit(0.9)])
    events = run(service, "hello")
    assert any(k == "done" and d.get("guardrail") == "greeting" for k, d in events)
    assert llm.calls == []


def test_new_conversation_is_created_and_saved():
    service, _, convs = make([hit(0.9)])
    events = run(service, "How is it deployed?")
    saved = convs.data[meta(events)["conversation_id"]]
    assert saved[0] == {"role": "user", "content": "How is it deployed?", "sources": None, "error": None}
    assert saved[1]["content"] == "answer [1]"
    assert saved[1]["sources"][0]["title"] == "a.md"


def test_done_event_carries_the_answer_id_for_rating():
    service, _, _ = make([hit(0.9)])
    done = next(d for k, d in run(service, "How is it deployed?") if k == "done")
    assert done["message_id"] == 2
    service, _, _ = make([hit(0.1)])
    done = next(d for k, d in run(service, "Unrelated question") if k == "done")
    assert done["guardrail"] == "no_context" and done["message_id"] == 2


def test_follow_up_uses_saved_history_and_rewrites():
    service, llm, convs = make([hit(0.9)])
    cid = meta(run(service, "How is it deployed?"))["conversation_id"]
    events = run(service, "and in production?", cid)
    assert meta(events)["conversation_id"] == cid
    sent = sent_messages(llm)
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert sent[0]["content"] == "How is it deployed?"
    # Every question is prepared; the follow-up one sees the conversation
    assert len(llm.rewrites) == 2
    assert "How is it deployed?" in llm.rewrites[1][0]["content"]
    assert len(convs.data[cid]) == 4


def test_search_is_prepared_and_the_answer_sees_the_interpretation():
    service, llm, _ = make([hit(0.9)], prepared={
        "corrected": "refund for a delayed delivery", "standalone": "refund policy for a delayed delivery",
        "queries": ["refund policy", "delivery delay compensation"], "missing": []})
    events = run(service, "refnd for a delayed delivry")
    done = next(d for k, d in events if k == "done")
    assert done["queries"] == ["refnd for a delayed delivry", "refund policy for a delayed delivery",
                               "refund policy", "delivery delay compensation"]
    assert service.embedder.queries[-1] == done["queries"]
    content = sent_messages(llm)[-1]["content"]
    # The answering model sees what the user typed and how it was interpreted, to check it
    assert "refnd for a delayed delivry" in content
    assert "refund policy for a delayed delivery" in content and "delivery delay compensation" in content


def test_failed_preparation_searches_the_question_as_typed():
    service, llm, _ = make([hit(0.9)], prepared=RuntimeError("down"))
    done = next(d for k, d in run(service, "How is it deployed?") if k == "done")
    assert done["queries"] == ["How is it deployed?"]
    assert len(llm.calls) == 1


def test_preparation_can_be_turned_off():
    service, llm, _ = make([hit(0.9)], settings_kwargs={"query_preparation": False})
    done = next(d for k, d in run(service, "How is it deployed?") if k == "done")
    assert done["queries"] == ["How is it deployed?"]
    assert llm.rewrites == []


def test_user_context_reaches_preparation_and_answer_and_boosts_its_documents():
    local = Hit(score=0.7, text="local", payload={"doc_id": "folder:local.pdf", "chunk_index": 0, "title": "local.pdf",
                                                  "source": "folder", "jurisdiction": "North"})
    service, llm, _ = make([hit(0.9), local])
    context = [{"id": "region", "label": "Region", "value": "North", "field": "jurisdiction"},
               {"id": "plan", "label": "Plan", "value": "Basic", "field": None}]
    run(service, "Opening hours?", context=context)
    assert "Region: North; Plan: Basic" in llm.rewrites[-1][0]["content"]
    assert "Region: North; Plan: Basic" in sent_messages(llm)[-1]["content"]
    # Plain searches plus the restricted ones (first two queries) for the boosted selector only
    assert [w for w in service.store.wheres if w] == [{"jurisdiction": "North"}] * 2


def test_admin_choice_overrides_the_default_provider():
    service, _, _ = make([hit(0.9)])
    other = FakeLLM()
    other.name = "other"
    service.llms["other"] = other
    assert service.current_default() == "fake"
    service.default_override = "other"
    assert service.pick_llm(None) is other
    service.default_override = "gone"
    assert service.current_default() == "fake"


def test_unknown_conversation_starts_a_new_one():
    service, _, convs = make([hit(0.9)])
    events = run(service, "question", "does-not-exist")
    assert meta(events)["conversation_id"] in convs.data


def test_failed_answer_is_saved_with_error_and_left_out_of_history():
    service, llm, convs = make([hit(0.9)], fail=True)
    try:
        run(service, "first")
    except RuntimeError:
        pass
    cid = next(iter(convs.data))
    assert convs.data[cid][-1]["error"] == "RuntimeError: cut off"
    assert convs.data[cid][-1]["content"] == "answer "
    llm.fail = False
    run(service, "second", cid)
    # The question whose answer failed is not sent as history
    assert [m["role"] for m in sent_messages(llm)] == ["user"]


def test_remote_provider_gets_redacted_question():
    service, llm, convs = make([hit(0.9)], remote=True)
    run(service, "Does password=Sup3rS3cretXy work to log in?")
    sent = sent_messages(llm)[-1]["content"]
    assert "Sup3rS3cretXy" not in sent
    assert "[REDACTED]" in sent
    # What is saved is what the user typed
    assert "Sup3rS3cretXy" in next(iter(convs.data.values()))[0]["content"]


def test_local_provider_gets_question_as_is():
    service, llm, _ = make([hit(0.9)], remote=False)
    run(service, "Does password=Sup3rS3cretXy work to log in?")
    assert "Sup3rS3cretXy" in sent_messages(llm)[-1]["content"]


def test_ollama_is_not_offered_when_claude_is_available():
    from rag.llm import build_llms

    names = lambda **kw: set(build_llms(Settings(_env_file=None, **kw), spend=None))  # noqa: E731
    assert names(anthropic_api_key="") == {"ollama"}
    assert names(anthropic_api_key="sk-ant-x") == {"anthropic"}
    assert names(anthropic_api_key="sk-ant-x", ollama_with_claude=True) == {"anthropic", "ollama"}
    assert names(anthropic_api_key="", ollama_url="") == set()


def test_default_provider_auto():
    assert Settings(_env_file=None, anthropic_api_key="").default_provider == "ollama"
    assert Settings(_env_file=None, anthropic_api_key="sk-ant-x").default_provider == "anthropic"
    assert Settings(_env_file=None, dry_run=True).default_provider == "anthropic"
    assert Settings(_env_file=None, llm_provider="ollama", anthropic_api_key="sk-ant-x").default_provider == "ollama"
