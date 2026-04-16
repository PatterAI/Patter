"""Tests for the Anthropic / Groq / Cerebras / Google LLM providers.

All tests in this module are MOCK unit tests: the vendor SDK clients
(``anthropic.AsyncAnthropic``, ``openai.AsyncOpenAI``,
``google.genai.Client``) are replaced with fakes that yield synthetic
event streams with the same shape the real APIs return.  This lets us
verify that each Patter provider correctly translates vendor-specific
events into Patter's ``{"type": "text" | "tool_call" | "done"}`` chunk
protocol without making network calls.

Integration tests that hit real APIs should live in
``tests/integration/`` and skip when the matching env var is absent.
"""

from __future__ import annotations

import json
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _collect(async_iter):
    out = []
    async for item in async_iter:
        out.append(item)
    return out


def _install_fake_module(name: str, module: types.ModuleType) -> None:
    """Install a module into ``sys.modules`` for vendor-SDK stubbing."""
    sys.modules[name] = module


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


class _FakeAnthropicStreamContext:
    """Async context manager that yields pre-defined stream events.

    Matches the shape of ``anthropic.AsyncAnthropic.messages.stream()``.
    """

    def __init__(self, events: list):
        self._events = events

    async def __aenter__(self):
        events = self._events

        class _Iter:
            def __aiter__(self):
                return self

            async def __anext__(self):
                if not events:
                    raise StopAsyncIteration
                return events.pop(0)

        return _Iter()

    async def __aexit__(self, *args):
        return None


class _FakeAnthropicMessages:
    def __init__(self, events: list):
        self._events = events

    def stream(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeAnthropicStreamContext(list(self._events))


class _FakeAnthropicClient:
    def __init__(self, *args, **kwargs):
        self.messages = _FakeAnthropicMessages(_FakeAnthropicClient.events)


@pytest.fixture
def patch_anthropic(monkeypatch):
    """Install a fake ``anthropic`` module so ``import anthropic`` works."""
    fake = types.ModuleType("anthropic")

    def _factory(events):
        _FakeAnthropicClient.events = events
        return _FakeAnthropicClient

    fake.AsyncAnthropic = _FakeAnthropicClient  # type: ignore[attr-defined]
    _install_fake_module("anthropic", fake)

    # Also drop the cached import of our provider module so it picks up
    # the fake module on next import.
    sys.modules.pop("patter.providers.anthropic_llm", None)

    yield _factory

    sys.modules.pop("anthropic", None)
    sys.modules.pop("patter.providers.anthropic_llm", None)


@pytest.mark.asyncio
async def test_anthropic_text_stream(patch_anthropic):
    """AnthropicLLMProvider emits text chunks and a final done chunk.

    MOCK: the anthropic SDK is stubbed to yield a realistic sequence of
    ``message_start`` / ``content_block_start`` / ``content_block_delta``
    (``text_delta``) / ``content_block_stop`` / ``message_stop`` events.
    """
    events = [
        SimpleNamespace(type="message_start"),
        SimpleNamespace(
            type="content_block_start",
            content_block=SimpleNamespace(type="text"),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text="Hello "),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text="world!"),
        ),
        SimpleNamespace(type="content_block_stop"),
        SimpleNamespace(type="message_stop"),
    ]
    patch_anthropic(events)

    from patter.providers.anthropic_llm import AnthropicLLMProvider

    provider = AnthropicLLMProvider(api_key="sk-test", model="claude-3-5-sonnet-20241022")
    chunks = await _collect(
        provider.stream(
            [
                {"role": "system", "content": "Be concise."},
                {"role": "user", "content": "Hi"},
            ]
        )
    )

    assert chunks == [
        {"type": "text", "content": "Hello "},
        {"type": "text", "content": "world!"},
        {"type": "done"},
    ]


@pytest.mark.asyncio
async def test_anthropic_tool_call_stream(patch_anthropic):
    """AnthropicLLMProvider emits tool_call chunks for ``tool_use`` blocks.

    MOCK: the anthropic SDK is stubbed to yield a ``tool_use``
    ``content_block_start`` followed by streamed ``input_json_delta``
    events, matching the real Anthropic streaming format.
    """
    events = [
        SimpleNamespace(type="message_start"),
        SimpleNamespace(
            type="content_block_start",
            content_block=SimpleNamespace(
                type="tool_use", id="toolu_01", name="get_weather"
            ),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="input_json_delta", partial_json='{"city":'),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="input_json_delta", partial_json='"Paris"}'),
        ),
        SimpleNamespace(type="content_block_stop"),
        SimpleNamespace(type="message_stop"),
    ]
    patch_anthropic(events)

    from patter.providers.anthropic_llm import AnthropicLLMProvider

    provider = AnthropicLLMProvider(api_key="sk-test")
    chunks = await _collect(
        provider.stream(
            [
                {"role": "system", "content": "You call tools."},
                {"role": "user", "content": "Weather in Paris?"},
            ],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get weather",
                        "parameters": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                        },
                    },
                }
            ],
        )
    )

    # Expected: initial tool_call with empty args, then two argument
    # delta chunks, then done.
    assert chunks[0] == {
        "type": "tool_call",
        "index": 0,
        "id": "toolu_01",
        "name": "get_weather",
        "arguments": "",
    }
    assert chunks[1] == {
        "type": "tool_call",
        "index": 0,
        "id": "toolu_01",
        "name": None,
        "arguments": '{"city":',
    }
    assert chunks[2] == {
        "type": "tool_call",
        "index": 0,
        "id": "toolu_01",
        "name": None,
        "arguments": '"Paris"}',
    }
    assert chunks[-1] == {"type": "done"}


# ---------------------------------------------------------------------------
# OpenAI-compatible providers (Groq + Cerebras) share a streaming path
# via OpenAILLMProvider, so we build one fake AsyncOpenAI client.
# ---------------------------------------------------------------------------


class _FakeChoicesDelta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, delta):
        self.delta = delta


class _FakeChatChunk:
    def __init__(self, choices):
        self.choices = choices


class _FakeAsyncStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


class _FakeCompletions:
    def __init__(self, chunks):
        self._chunks = chunks

    async def create(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeAsyncStream(self._chunks)


class _FakeChat:
    def __init__(self, chunks):
        self.completions = _FakeCompletions(chunks)


class _FakeAsyncOpenAI:
    last_kwargs = None
    instances: list = []

    def __init__(self, *args, **kwargs):
        self.init_kwargs = kwargs
        self.chat = _FakeChat(_FakeAsyncOpenAI.chunks)
        _FakeAsyncOpenAI.instances.append(self)


@pytest.fixture
def patch_openai(monkeypatch):
    """Install a fake ``openai`` module."""
    fake = types.ModuleType("openai")
    fake.AsyncOpenAI = _FakeAsyncOpenAI  # type: ignore[attr-defined]

    # Cerebras imports submodules from openai; stub them too.
    models_mod = types.ModuleType("openai._models")
    class FinalRequestOptions:  # noqa: D401
        pass
    models_mod.FinalRequestOptions = FinalRequestOptions  # type: ignore[attr-defined]

    utils_mod = types.ModuleType("openai._utils")
    utils_mod.is_mapping = lambda x: isinstance(x, dict)  # type: ignore[attr-defined]

    _install_fake_module("openai", fake)
    _install_fake_module("openai._models", models_mod)
    _install_fake_module("openai._utils", utils_mod)

    sys.modules.pop("patter.providers.groq_llm", None)
    sys.modules.pop("patter.providers.cerebras_llm", None)

    def _setup(chunks):
        _FakeAsyncOpenAI.chunks = chunks
        _FakeAsyncOpenAI.instances = []
        return _FakeAsyncOpenAI

    yield _setup

    sys.modules.pop("openai", None)
    sys.modules.pop("openai._models", None)
    sys.modules.pop("openai._utils", None)
    sys.modules.pop("patter.providers.groq_llm", None)
    sys.modules.pop("patter.providers.cerebras_llm", None)


@pytest.mark.asyncio
async def test_groq_text_stream(patch_openai):
    """GroqLLMProvider forwards OpenAI-style chunks unchanged.

    MOCK: ``openai.AsyncOpenAI`` is stubbed with a chat.completions.create
    that returns a synthetic stream of chunks shaped like Groq's (and
    OpenAI's) Chat Completions streaming format.
    """
    chunks = [
        _FakeChatChunk(
            [_FakeChoice(_FakeChoicesDelta(content="Hi "))]
        ),
        _FakeChatChunk(
            [_FakeChoice(_FakeChoicesDelta(content="there!"))]
        ),
    ]
    patch_openai(chunks)

    from patter.providers.groq_llm import GroqLLMProvider

    provider = GroqLLMProvider(api_key="gsk-test", model="llama-3.3-70b-versatile")
    out = await _collect(
        provider.stream(
            [
                {"role": "system", "content": "x"},
                {"role": "user", "content": "hi"},
            ]
        )
    )
    assert out == [
        {"type": "text", "content": "Hi "},
        {"type": "text", "content": "there!"},
    ]

    # Client was built with Groq base URL
    assert _FakeAsyncOpenAI.instances[0].init_kwargs["base_url"] == (
        "https://api.groq.com/openai/v1"
    )


@pytest.mark.asyncio
async def test_groq_requires_api_key(patch_openai, monkeypatch):
    """Groq raises ValueError when no key is provided and env var is unset."""
    patch_openai([])
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    from patter.providers.groq_llm import GroqLLMProvider

    with pytest.raises(ValueError, match="Groq API key"):
        GroqLLMProvider()


@pytest.mark.asyncio
async def test_cerebras_falls_back_without_compression(patch_openai):
    """CerebrasLLMProvider uses the stock AsyncOpenAI when compression off.

    MOCK: reuses the same OpenAI fake as Groq — Cerebras is
    OpenAI-compatible.
    """
    chunks = [
        _FakeChatChunk(
            [_FakeChoice(_FakeChoicesDelta(content="Cerebras fast."))]
        ),
    ]
    patch_openai(chunks)

    from patter.providers.cerebras_llm import CerebrasLLMProvider

    provider = CerebrasLLMProvider(
        api_key="cb-test",
        model="llama3.1-8b",
        gzip_compression=False,
        msgpack_encoding=False,
    )
    out = await _collect(
        provider.stream([{"role": "user", "content": "hi"}])
    )
    assert out == [{"type": "text", "content": "Cerebras fast."}]
    assert _FakeAsyncOpenAI.instances[0].init_kwargs["base_url"] == (
        "https://api.cerebras.ai/v1"
    )


# ---------------------------------------------------------------------------
# Google Gemini
# ---------------------------------------------------------------------------


class _FakePart:
    def __init__(self, text=None, function_call=None):
        self.text = text
        self.function_call = function_call


class _FakeContent:
    def __init__(self, parts):
        self.parts = parts


class _FakeCandidate:
    def __init__(self, content):
        self.content = content


class _FakeGenResponse:
    def __init__(self, candidates):
        self.candidates = candidates


class _FakeFunctionCall:
    def __init__(self, name, args, id=None):
        self.name = name
        self.args = args
        self.id = id


class _FakeModels:
    def __init__(self, responses):
        self._responses = responses
        self.last_call: dict | None = None

    async def generate_content_stream(self, *, model, contents, config=None):
        self.last_call = {"model": model, "contents": contents, "config": config}

        async def _gen():
            for r in self._responses:
                yield r

        return _gen()


class _FakeAio:
    def __init__(self, responses):
        self.models = _FakeModels(responses)


class _FakeGeminiClient:
    def __init__(self, *args, **kwargs):
        self.init_kwargs = kwargs
        self.aio = _FakeAio(_FakeGeminiClient.responses)


@pytest.fixture
def patch_google(monkeypatch):
    """Install fake google.genai modules."""
    genai_mod = types.ModuleType("google.genai")
    genai_mod.Client = _FakeGeminiClient  # type: ignore[attr-defined]

    types_mod = types.ModuleType("google.genai.types")

    class _StubDataclass:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

    class Part(_StubDataclass):
        pass

    class Content(_StubDataclass):
        pass

    class Tool(_StubDataclass):
        pass

    class FunctionDeclaration(_StubDataclass):
        pass

    class FunctionCall(_StubDataclass):
        pass

    class FunctionResponse(_StubDataclass):
        pass

    class GenerateContentConfig(_StubDataclass):
        pass

    types_mod.Part = Part  # type: ignore[attr-defined]
    types_mod.Content = Content  # type: ignore[attr-defined]
    types_mod.Tool = Tool  # type: ignore[attr-defined]
    types_mod.FunctionDeclaration = FunctionDeclaration  # type: ignore[attr-defined]
    types_mod.FunctionCall = FunctionCall  # type: ignore[attr-defined]
    types_mod.FunctionResponse = FunctionResponse  # type: ignore[attr-defined]
    types_mod.GenerateContentConfig = GenerateContentConfig  # type: ignore[attr-defined]

    google_mod = types.ModuleType("google")
    google_mod.genai = genai_mod  # type: ignore[attr-defined]

    _install_fake_module("google", google_mod)
    _install_fake_module("google.genai", genai_mod)
    _install_fake_module("google.genai.types", types_mod)

    sys.modules.pop("patter.providers.google_llm", None)

    def _setup(responses):
        _FakeGeminiClient.responses = responses
        return _FakeGeminiClient

    yield _setup

    for name in (
        "google",
        "google.genai",
        "google.genai.types",
        "patter.providers.google_llm",
    ):
        sys.modules.pop(name, None)


@pytest.mark.asyncio
async def test_google_text_stream(patch_google):
    """GoogleLLMProvider emits text chunks + done.

    MOCK: ``google.genai.Client`` is stubbed with a fake
    ``generate_content_stream`` that yields shaped responses mirroring
    the real Gemini streaming format.
    """
    responses = [
        _FakeGenResponse([_FakeCandidate(_FakeContent([_FakePart(text="Hello ")]))]),
        _FakeGenResponse([_FakeCandidate(_FakeContent([_FakePart(text="Gemini!")]))]),
    ]
    patch_google(responses)

    from patter.providers.google_llm import GoogleLLMProvider

    provider = GoogleLLMProvider(api_key="AIza-test", model="gemini-2.5-flash")
    chunks = await _collect(
        provider.stream(
            [
                {"role": "system", "content": "Be kind."},
                {"role": "user", "content": "Hi"},
            ]
        )
    )
    assert chunks == [
        {"type": "text", "content": "Hello "},
        {"type": "text", "content": "Gemini!"},
        {"type": "done"},
    ]


@pytest.mark.asyncio
async def test_google_tool_call_stream(patch_google):
    """GoogleLLMProvider emits tool_call chunks for function_call parts.

    MOCK: Gemini returns a ``function_call`` part; the provider should
    translate it into a ``tool_call`` chunk with JSON-encoded arguments.
    """
    fn_call = _FakeFunctionCall(name="get_weather", args={"city": "Paris"}, id="gc1")
    responses = [
        _FakeGenResponse([_FakeCandidate(_FakeContent([_FakePart(function_call=fn_call)]))]),
    ]
    patch_google(responses)

    from patter.providers.google_llm import GoogleLLMProvider

    provider = GoogleLLMProvider(api_key="AIza-test")
    chunks = await _collect(
        provider.stream(
            [
                {"role": "user", "content": "weather?"},
            ],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get weather",
                        "parameters": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                        },
                    },
                }
            ],
        )
    )

    assert chunks[0]["type"] == "tool_call"
    assert chunks[0]["name"] == "get_weather"
    assert chunks[0]["id"] == "gc1"
    assert json.loads(chunks[0]["arguments"]) == {"city": "Paris"}
    assert chunks[-1] == {"type": "done"}


@pytest.mark.asyncio
async def test_google_requires_api_key(patch_google, monkeypatch):
    """Google raises ValueError when no key is provided and env is unset."""
    patch_google([])
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)

    from patter.providers.google_llm import GoogleLLMProvider

    with pytest.raises(ValueError, match="Google API key"):
        GoogleLLMProvider(vertexai=False)
