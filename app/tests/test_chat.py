import asyncio

from rag.chat import ChatService
from rag.config import Settings
from rag.i18n import t
from rag.vectorstore import Hit


class FakeEmbedder:
    def embed_query(self, text):
        return [0.0]


class FakeStore:
    def __init__(self, hits):
        self.hits = hits

    def search(self, vector, limit, exclude_statuses=None):
        self.excluded = exclude_statuses
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

    def __init__(self, remote=False, fail=False):
        self.remote = remote
        self.fail = fail
        self.calls = []
        self.rewrites = []

    async def stream(self, system, messages):
        self.calls.append((system, messages))
        yield "answer "
        if self.fail:
            raise RuntimeError("cut off")
        yield "[1]"

    async def complete(self, system, messages):
        self.rewrites.append(messages)
        return "rewritten query"


def run(service, question, conversation_id=None):
    async def collect():
        return [e async for e in service.answer(question, conversation_id, "fake")]
    return asyncio.run(collect())


def make(hits, language="en", **llm_kwargs):
    llm = FakeLLM(**llm_kwargs)
    convs = FakeConversations()
    settings = Settings(_env_file=None, min_score=0.5, language=language)
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
    assert len(llm.rewrites) == 1
    assert len(convs.data[cid]) == 4


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
