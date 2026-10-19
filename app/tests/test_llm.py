import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from rag.config import Settings
from rag.embeddings import document_prefix, query_prefix
from rag.llm import build_llms
from rag.llm.anthropic_llm import FALLBACK_BETA, AnthropicLLM, with_cache_points
from rag.llm.deepseek_llm import DeepSeekLLM
from rag.llm.spending import BudgetExhausted, PriceTable, Usage, default_prices, usage_by_model


# ---------------------------------------------------------------- embeddings and collection

def test_query_prefix_for_instruct_models():
    assert query_prefix("Qwen/Qwen3-Embedding-0.6B", "Search").startswith("Instruct: Search\nQuery:")
    assert query_prefix("intfloat/multilingual-e5-large", "x") == "query: "
    assert query_prefix("BAAI/bge-m3", "x") == ""
    assert document_prefix("Qwen/Qwen3-Embedding-0.6B") == ""
    assert document_prefix("intfloat/multilingual-e5-large") == "passage: "


def test_collection_name_depends_on_model():
    a = Settings(_env_file=None, embedding_model="Qwen/Qwen3-Embedding-0.6B")
    b = Settings(_env_file=None, embedding_model="BAAI/bge-m3")
    assert a.collection_name == "documents__qwen-qwen3-embedding-0-6b__bm25"
    assert a.collection_name != b.collection_name
    # Without BM25 the points have no sparse vector: another collection
    assert Settings(_env_file=None, sparse_search=False).collection_name == "documents__qwen-qwen3-embedding-0-6b"


# ---------------------------------------------------------------- prices

def test_cost_uses_cache_prices():
    prices = default_prices()
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000, cache_write=1_000_000, cache_read=1_000_000)
    assert prices.cost("claude-opus-5-5", usage) == pytest.approx(4 + 20 + 5 + 0.2)


def test_haiku_long_prompt_tier():
    prices = default_prices()
    short = prices.cost("claude-haiku-5-5", Usage(input_tokens=50_000))
    long = prices.cost("claude-haiku-5-5", Usage(input_tokens=200_000))
    assert short == pytest.approx(50_000 * 0.10 / 1e6)
    assert long == pytest.approx(200_000 * 0.50 / 1e6)


def test_unknown_model_is_priced_as_most_expensive():
    prices = PriceTable({"models": {
        "cheap": {"tiers": [{"up_to_input_tokens": None, "input": 1, "cache_write_5m": 1, "cache_read": 1, "output": 1}]},
        "expensive": {"tiers": [{"up_to_input_tokens": None, "input": 9, "cache_write_5m": 9, "cache_read": 9, "output": 9}]},
    }})
    assert prices.cost("other", Usage(output_tokens=1_000_000)) == 9


def test_usage_with_fallback_iterations():
    it1 = SimpleNamespace(type="message", model="claude-opus-5-5", input_tokens=100, output_tokens=0)
    it2 = SimpleNamespace(type="fallback_message", model="claude-opus-5", input_tokens=100, output_tokens=50)
    msg = SimpleNamespace(model="claude-opus-5", usage=SimpleNamespace(iterations=[it1, it2]))
    assert [m for m, _ in usage_by_model(msg)] == ["claude-opus-5-5", "claude-opus-5"]


# ---------------------------------------------------------------- Claude

def test_cache_points_on_system_and_last_history_message():
    system, msgs = with_cache_points("sys", [
        {"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}, {"role": "user", "content": "docs+q2"},
    ])
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert msgs[1]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert msgs[2]["content"] == "docs+q2"  # the only part that changes every turn goes last


class FakeSpend:
    def __init__(self, exhausted=False):
        self.exhausted = exhausted
        self.records = []

    def check(self, model, input_chars, max_tokens, purpose):
        if self.exhausted:
            raise BudgetExhausted("no budget left")

    def record(self, message, purpose, simulated=False):
        self.records.append((message.model, purpose))
        return 0.01


class FakeStream:
    def __init__(self, calls, kwargs):
        calls.append(kwargs)
        self.final = SimpleNamespace(model=kwargs["model"], stop_reason="end_turn",
                                     usage=SimpleNamespace(iterations=None, input_tokens=10, output_tokens=5,
                                                           cache_read_input_tokens=0),
                                     content=[SimpleNamespace(type="text", text="ok")])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def text_stream(self):
        async def gen():
            yield "ok"
        return gen()

    async def get_final_message(self):
        return self.final


class FakeClient:
    def __init__(self, reply='{"corrected": "q", "standalone": "q", "queries": [], "missing": []}',
                 stop_reason="end_turn"):
        self.calls = []

        async def create(**kw):
            self.calls.append(kw)
            return SimpleNamespace(model=kw["model"], stop_reason=stop_reason,
                                   usage=SimpleNamespace(iterations=None, input_tokens=10, output_tokens=5,
                                                         cache_read_input_tokens=0),
                                   content=[SimpleNamespace(type="text", text=reply)])

        self.messages = SimpleNamespace(stream=lambda **kw: FakeStream(self.calls, kw), create=create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: FakeStream(self.calls, kw)))


def make_claude(spend=None, **client_kwargs):
    client = FakeClient(**client_kwargs)
    llm = AnthropicLLM(Settings(_env_file=None, anthropic_api_key="x"), spend or FakeSpend(), client=client)
    return llm, client


async def collect(gen):
    return [x async for x in gen]


def test_answer_uses_fallbacks_and_records_spend():
    llm, client = make_claude()
    out = asyncio.run(collect(llm.stream("sys", [{"role": "user", "content": "hello"}])))
    assert out == ["ok"]
    call = client.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["betas"] == [FALLBACK_BETA] and call["fallbacks"] == "default"
    assert llm.spend.records == [("claude-opus-5-5", "answer")]


def test_search_preparation_uses_fast_model_with_json_schema():
    llm, client = make_claude()
    schema = {"type": "object"}
    data = asyncio.run(llm.complete_json("sys", [{"role": "user", "content": "x"}], schema))
    assert data == {"corrected": "q", "standalone": "q", "queries": [], "missing": []}
    call = client.calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert "fallbacks" not in call
    assert call["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": schema}}
    assert llm.spend.records == [("claude-haiku-5-5", "prepare")]


def test_unusable_preparation_returns_empty():
    for kwargs in ({"reply": "not json"}, {"stop_reason": "refusal"}, {"stop_reason": "max_tokens"}):
        llm, _ = make_claude(**kwargs)
        assert asyncio.run(llm.complete_json("sys", [{"role": "user", "content": "x"}], {})) == {}


def test_budget_exhausted_blocks_the_call():
    llm, client = make_claude(FakeSpend(exhausted=True))
    with pytest.raises(BudgetExhausted):
        asyncio.run(collect(llm.stream("sys", [{"role": "user", "content": "x"}])))
    assert client.calls == []


# ---------------------------------------------------------------- DeepSeek

class DeepSpend:
    def __init__(self):
        self.prices = default_prices()
        self.records = []
        self.checks = []

    def check(self, model, input_chars, max_tokens, purpose):
        self.checks.append((model, purpose))

    def record_usage(self, model, purpose, usage, cost, simulated):
        self.records.append((model, purpose, usage, cost))


def make_deepseek(handler, spend=None):
    client = httpx.AsyncClient(
        base_url="https://api.deepseek.com/v1/",
        headers={"Authorization": "Bearer x"},
        transport=httpx.MockTransport(handler),
    )
    llm = DeepSeekLLM(Settings(_env_file=None, deepseek_api_key="x"), spend or DeepSpend(), client=client)
    return llm


def test_deepseek_complete_json_uses_json_mode_and_records_usage():
    def handler(request):
        body = json.loads(request.content)
        assert body["model"] == "deepseek-flash"
        assert body["response_format"] == {"type": "json_object"}
        assert body["thinking"] == {"type": "disabled"}
        assert request.url.path == "/v1/chat/completions"
        system = body["messages"][0]["content"]
        assert "json" in system.lower() and '"corrected"' in system
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"corrected": "q", "standalone": "q", "queries": [], "missing": []}'}}],
            "usage": {"prompt_tokens": 100, "prompt_cache_hit_tokens": 20,
                      "prompt_cache_miss_tokens": 80, "completion_tokens": 10},
        })

    llm = make_deepseek(handler)
    schema = {"type": "object", "properties": {"corrected": {"type": "string"}}}
    data = asyncio.run(llm.complete_json("sys", [{"role": "user", "content": "x"}], schema))
    assert data["corrected"] == "q"
    model, purpose, usage, cost = llm.spend.records[0]
    assert (model, purpose) == ("deepseek-flash", "prepare")
    assert usage.input_tokens == 80 and usage.cache_read == 20 and usage.output_tokens == 10
    assert cost > 0


def test_deepseek_stream_yields_tokens_and_records_usage():
    def handler(request):
        body = json.loads(request.content)
        assert body["stream"] is True
        assert body["stream_options"] == {"include_usage": True}
        assert body["thinking"] == {"type": "disabled"}
        sse = (
            'data: {"choices":[{"delta":{"content":"Hola"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":" mundo"}}]}\n\n'
            'data: {"choices":[],"usage":{"prompt_tokens":50,"prompt_cache_hit_tokens":0,'
            '"prompt_cache_miss_tokens":50,"completion_tokens":2}}\n\n'
            'data: [DONE]\n\n'
        )
        return httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})

    llm = make_deepseek(handler)
    out = asyncio.run(collect(llm.stream("sys", [{"role": "user", "content": "x"}])))
    assert out == ["Hola", " mundo"]
    assert llm.spend.records[0][0:2] == ("deepseek-flash", "answer")


def test_build_llms_adds_deepseek_and_skips_ollama():
    settings = Settings(_env_file=None, deepseek_api_key="x", ollama_url="http://ollama:11434")
    assert set(build_llms(settings, FakeSpend())) == {"deepseek"}


def test_deepseek_is_the_default_provider_when_configured():
    assert Settings(_env_file=None, deepseek_api_key="x").default_provider == "deepseek"
    assert Settings(_env_file=None, llm_provider="deepseek").default_provider == "deepseek"


def test_deepseek_price():
    prices = default_prices()
    assert prices.cost("deepseek-flash", Usage(input_tokens=1_000_000, output_tokens=1_000_000)) == pytest.approx(0.30 + 1.20)
    assert prices.cost("deepseek-v4-pro", Usage(input_tokens=1_000_000, output_tokens=1_000_000)) == pytest.approx(1.32 + 3.96)
