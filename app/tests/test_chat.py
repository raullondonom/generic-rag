import asyncio

from rag.chat import ChatService
from rag.config import Settings
from rag.prompts import NO_CONTEXT_ANSWER
from rag.vectorstore import Hit


class FakeEmbedder:
    def embed_one(self, text):
        return [0.0]


class FakeStore:
    def __init__(self, hits):
        self.hits = hits

    def search(self, vector, limit):
        return self.hits


class FakeLLM:
    name = "fake"
    model = "fake-model"

    def __init__(self):
        self.calls = []

    async def stream(self, system, messages):
        self.calls.append(messages)
        yield "respuesta [1]"

    async def complete(self, system, messages):
        return "consulta reescrita"


def run(service, question, history=None):
    async def collect():
        return [e async for e in service.answer(question, history or [], "fake")]
    return asyncio.run(collect())


def make(hits):
    llm = FakeLLM()
    service = ChatService(Settings(min_score=0.5), FakeEmbedder(), FakeStore(hits), {"fake": llm})
    return service, llm


def hit(score, doc_id="folder:a.md"):
    return Hit(score=score, text="contenido", payload={"doc_id": doc_id, "title": "a.md", "source": "folder"})


def test_guardrail_without_relevant_context_does_not_call_llm():
    service, llm = make([hit(0.2)])
    events = run(service, "¿Cuál es la capital de Francia?")
    tokens = "".join(d for k, d in events if k == "token")
    assert tokens == NO_CONTEXT_ANSWER
    assert llm.calls == []


def test_answer_with_context_calls_llm_with_documents():
    service, llm = make([hit(0.8), hit(0.7, "folder:b.md")])
    events = run(service, "¿Cómo se despliega?")
    sources = next(d for k, d in events if k == "sources")
    assert [s["n"] for s in sources] == [1, 2]
    assert len(llm.calls) == 1
    assert "<documentos>" in llm.calls[0][-1]["content"]


def test_greeting_short_circuit():
    service, llm = make([hit(0.9)])
    events = run(service, "hola")
    assert any(k == "done" and d.get("guardrail") == "greeting" for k, d in events)
    assert llm.calls == []


def test_history_starts_with_user():
    service, llm = make([hit(0.9)])
    run(service, "¿y eso?", [{"role": "assistant", "content": "x"}, {"role": "user", "content": "q"},
                            {"role": "assistant", "content": "r"}])
    assert llm.calls[0][0]["role"] == "user"
