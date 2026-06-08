"""Tests for the generic OpenAI-compatible LLM provider.

These exercise the REAL provider: real construction of the ``AsyncOpenAI``
client (base URL / timeout / headers), real ``_build_completion_kwargs``
assembly, and real SSE-chunk normalisation. The ONLY mocked surface is the
paid external boundary — ``AsyncOpenAI.chat.completions.create`` — and that
test is tagged ``@pytest.mark.mocked``.
"""

from __future__ import annotations

import pytest

from getpatter.llm.openai_compatible import OpenAICompatibleLLMProvider
from getpatter.services.llm_loop import LLMProvider


def _base_url_str(provider: OpenAICompatibleLLMProvider) -> str:
    """Read the constructed client's base URL (AsyncOpenAI appends a slash)."""
    return str(provider._client.base_url)


@pytest.mark.unit
def test_openai_compatible_provider_points_client_at_base_url_with_timeout() -> None:
    provider = OpenAICompatibleLLMProvider(
        base_url="http://127.0.0.1:9/v1", model="m", timeout=120.0
    )
    # Real client carries the base URL and the long (non-default) timeout.
    assert _base_url_str(provider).startswith("http://127.0.0.1:9/v1")
    assert provider._client.timeout == 120.0
    assert provider._model == "m"
    # Satisfies the LLMProvider protocol.
    assert isinstance(provider, LLMProvider)


@pytest.mark.unit
def test_keyless_gateway_construction_does_not_raise(monkeypatch) -> None:
    # No api_key, no api_key_env — Ollama / vLLM / LM Studio keyless path.
    provider = OpenAICompatibleLLMProvider(
        base_url="http://127.0.0.1:11434/v1", model="llama3.1"
    )
    # The EMPTY sentinel keeps AsyncOpenAI (which rejects None) happy.
    assert provider._client.api_key == "EMPTY"


@pytest.mark.unit
def test_api_key_resolved_from_env_var(monkeypatch) -> None:
    monkeypatch.setenv("MY_GATEWAY_KEY", "secret-token-value")
    provider = OpenAICompatibleLLMProvider(
        base_url="http://127.0.0.1:9/v1",
        model="m",
        api_key_env="MY_GATEWAY_KEY",
    )
    # The resolved key reaches the client (and is never logged).
    assert provider._client.api_key == "secret-token-value"


@pytest.mark.unit
def test_explicit_api_key_wins_over_env(monkeypatch) -> None:
    monkeypatch.setenv("MY_GATEWAY_KEY", "from-env")
    provider = OpenAICompatibleLLMProvider(
        base_url="http://127.0.0.1:9/v1",
        model="m",
        api_key="explicit-key",
        api_key_env="MY_GATEWAY_KEY",
    )
    assert provider._client.api_key == "explicit-key"


@pytest.mark.unit
def test_session_user_prefix_off_by_default_omits_user_field() -> None:
    provider = OpenAICompatibleLLMProvider(base_url="http://127.0.0.1:9/v1", model="m")
    kwargs = provider._build_completion_kwargs(
        [{"role": "user", "content": "hi"}], None, call_id="abc"
    )
    # Backward compatible: no session prefix => no `user` field.
    assert "user" not in kwargs


@pytest.mark.unit
def test_session_user_prefix_emits_stable_call_user() -> None:
    provider = OpenAICompatibleLLMProvider(
        base_url="http://127.0.0.1:9/v1",
        model="m",
        session_user_prefix="patter-call-",
    )
    kwargs = provider._build_completion_kwargs(
        [{"role": "user", "content": "hi"}], None, call_id="abc"
    )
    assert kwargs["user"] == "patter-call-abc"


@pytest.mark.unit
def test_session_user_field_omitted_without_call_id() -> None:
    provider = OpenAICompatibleLLMProvider(
        base_url="http://127.0.0.1:9/v1",
        model="m",
        session_user_prefix="patter-call-",
    )
    kwargs = provider._build_completion_kwargs(
        [{"role": "user", "content": "hi"}], None, call_id=None
    )
    # Prefix set but no call id => still no `user` field.
    assert "user" not in kwargs


@pytest.mark.unit
def test_session_id_header_emits_prefixed_value_independent_of_user() -> None:
    """session_id_header + session_id_prefix produce
    extra_headers[name]=f'{prefix}{call_id}' WITHOUT needing the user field."""
    provider = OpenAICompatibleLLMProvider(
        base_url="http://127.0.0.1:9/v1",
        model="m",
        # Note: no session_user_prefix — the header stands alone.
        session_id_header="X-Hermes-Session-Id",
        session_id_prefix="patter-call-",
    )
    kwargs = provider._build_completion_kwargs(
        [{"role": "user", "content": "hi"}], None, call_id="abc"
    )
    assert kwargs["extra_headers"] == {"X-Hermes-Session-Id": "patter-call-abc"}
    # Decoupled: no user field because session_user_prefix is unset.
    assert "user" not in kwargs


@pytest.mark.unit
def test_session_key_header_emits_static_value_regardless_of_call_id() -> None:
    """session_key_header + session_key emit a STATIC header (no call_id
    interpolation), present even when no call_id is available."""
    provider = OpenAICompatibleLLMProvider(
        base_url="http://127.0.0.1:9/v1",
        model="m",
        session_key_header="X-Hermes-Session-Key",
        session_key="mem-scope-123",
    )
    # No call_id at all — the memory-scope header is per-call-independent.
    kwargs = provider._build_completion_kwargs(
        [{"role": "user", "content": "hi"}], None, call_id=None
    )
    assert kwargs["extra_headers"] == {"X-Hermes-Session-Key": "mem-scope-123"}


@pytest.mark.unit
def test_session_key_header_without_value_is_omitted() -> None:
    """session_key_header set but session_key None => header omitted (opt-in)."""
    provider = OpenAICompatibleLLMProvider(
        base_url="http://127.0.0.1:9/v1",
        model="m",
        session_key_header="X-Hermes-Session-Key",
        # session_key intentionally unset.
    )
    kwargs = provider._build_completion_kwargs(
        [{"role": "user", "content": "hi"}], None, call_id="abc"
    )
    assert "extra_headers" not in kwargs


@pytest.mark.unit
def test_all_three_signals_combine_without_clobbering_existing_headers() -> None:
    """user + session_id_header + session_key_header merge into one
    extra_headers dict that also preserves a pre-existing header."""
    provider = OpenAICompatibleLLMProvider(
        base_url="http://127.0.0.1:9/v1",
        model="m",
        session_user_prefix="patter-call-",
        session_id_header="X-Hermes-Session-Id",
        session_id_prefix="patter-call-",
        session_key_header="X-Hermes-Session-Key",
        session_key="mem-9",
    )
    # Simulate a pre-existing extra_headers on the kwargs (future-safe merge).
    import getpatter.services.llm_loop as base_mod

    orig = base_mod.OpenAILLMProvider._build_completion_kwargs

    def _with_existing(self, messages, tools):
        kw = orig(self, messages, tools)
        kw["extra_headers"] = {"X-Pre": "keep"}
        return kw

    base_mod.OpenAILLMProvider._build_completion_kwargs = _with_existing
    try:
        kwargs = provider._build_completion_kwargs(
            [{"role": "user", "content": "hi"}], None, call_id="abc"
        )
    finally:
        base_mod.OpenAILLMProvider._build_completion_kwargs = orig

    assert kwargs["user"] == "patter-call-abc"
    assert kwargs["extra_headers"] == {
        "X-Pre": "keep",
        "X-Hermes-Session-Id": "patter-call-abc",
        "X-Hermes-Session-Key": "mem-9",
    }


@pytest.mark.unit
def test_no_session_signals_is_byte_identical_to_parent() -> None:
    """None of the three signals set => no `user`, no `extra_headers`."""
    provider = OpenAICompatibleLLMProvider(base_url="http://127.0.0.1:9/v1", model="m")
    kwargs = provider._build_completion_kwargs(
        [{"role": "user", "content": "hi"}], None, call_id="abc"
    )
    assert "user" not in kwargs
    assert "extra_headers" not in kwargs


@pytest.mark.unit
def test_openclaw_shape_config_yields_raw_call_id_value() -> None:
    """Regression: OpenClaw uses session_id_header with an empty prefix, so the
    header value is the RAW call id — wire-identical to the old session_header
    behaviour."""
    provider = OpenAICompatibleLLMProvider(
        base_url="http://127.0.0.1:9/v1",
        model="m",
        session_user_prefix="patter-call-",
        session_id_header="x-openclaw-session-key",
        session_id_prefix="",
    )
    kwargs = provider._build_completion_kwargs(
        [{"role": "user", "content": "hi"}], None, call_id="abc"
    )
    assert kwargs["user"] == "patter-call-abc"
    assert kwargs["extra_headers"] == {"x-openclaw-session-key": "abc"}


@pytest.mark.unit
def test_extra_headers_merge_preserves_user_agent() -> None:
    provider = OpenAICompatibleLLMProvider(
        base_url="http://127.0.0.1:9/v1",
        model="m",
        extra_headers={"X-Foo": "1"},
    )
    headers = dict(provider._client.default_headers)
    assert headers.get("X-Foo") == "1"
    # User-Agent is the getpatter SDK attribution and must survive the merge.
    assert headers.get("User-Agent", "").startswith("getpatter/")


# ---------------------------------------------------------------------------
# Streaming — mocks ONLY the paid external boundary (chat.completions.create).
# ---------------------------------------------------------------------------


class _Delta:
    def __init__(self, content=None, tool_calls=None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, delta) -> None:
        self.delta = delta


class _Chunk:
    """Real OpenAI-SSE-shaped streaming chunk (the SDK yields these objects)."""

    def __init__(self, *, content=None, usage=None) -> None:
        self.choices = [_Choice(_Delta(content=content))] if content is not None else []
        self.usage = usage


class _Usage:
    def __init__(self, prompt_tokens, completion_tokens) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.prompt_tokens_details = None


class _FakeStream:
    """Async iterator over chunks, mimicking the AsyncOpenAI stream object."""

    def __init__(self, chunks) -> None:
        self._chunks = chunks

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for chunk in self._chunks:
            yield chunk

    async def close(self) -> None:  # pragma: no cover - not exercised here
        pass


@pytest.mark.mocked
async def test_stream_sends_user_field_and_speaks_content() -> None:
    """The create() call receives user='patter-call-<id>' and the inherited
    real SSE loop normalises chunks to the Patter chunk protocol."""
    provider = OpenAICompatibleLLMProvider(
        base_url="http://127.0.0.1:9/v1",
        model="m",
        session_user_prefix="patter-call-",
    )

    captured: dict = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return _FakeStream(
            [
                _Chunk(content="Hello"),
                _Chunk(content=" world"),
                _Chunk(usage=_Usage(prompt_tokens=10, completion_tokens=3)),
            ]
        )

    # Mock ONLY the paid external boundary.
    provider._client.chat.completions.create = fake_create

    chunks = []
    async for chunk in provider.stream(
        [{"role": "user", "content": "hi"}], None, call_id="xyz"
    ):
        chunks.append(chunk)

    # The per-call session user reached the wire.
    assert captured["user"] == "patter-call-xyz"

    text = "".join(c["content"] for c in chunks if c["type"] == "text")
    assert text == "Hello world"

    usage = [c for c in chunks if c["type"] == "usage"]
    assert len(usage) == 1
    assert usage[0]["input_tokens"] == 10
    assert usage[0]["output_tokens"] == 3


@pytest.mark.mocked
async def test_stream_without_session_prefix_omits_user_on_the_wire() -> None:
    provider = OpenAICompatibleLLMProvider(base_url="http://127.0.0.1:9/v1", model="m")

    captured: dict = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return _FakeStream([_Chunk(content="ok")])

    provider._client.chat.completions.create = fake_create

    async for _ in provider.stream(
        [{"role": "user", "content": "hi"}], None, call_id="xyz"
    ):
        pass

    # Backward compatible: no `user` field unless the caller opts in.
    assert "user" not in captured


@pytest.mark.mocked
async def test_stream_sends_session_id_header_on_the_wire() -> None:
    """A provider configured with session_id_header puts the per-call header
    onto the create() call via extra_headers — independent of the user field."""
    provider = OpenAICompatibleLLMProvider(
        base_url="http://127.0.0.1:9/v1",
        model="m",
        session_id_header="X-Hermes-Session-Id",
        session_id_prefix="patter-call-",
    )

    captured: dict = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return _FakeStream([_Chunk(content="ok")])

    provider._client.chat.completions.create = fake_create

    async for _ in provider.stream(
        [{"role": "user", "content": "hi"}], None, call_id="abc"
    ):
        pass

    assert captured["extra_headers"] == {"X-Hermes-Session-Id": "patter-call-abc"}
    # No user field — session_user_prefix unset.
    assert "user" not in captured


async def _capture_warmup_headers(provider, monkeypatch) -> dict:
    """Run warmup() with only the httpx GET boundary mocked, returning the
    real headers the provider assembled."""
    import httpx

    captured: dict = {}

    async def fake_get(self, url, headers=None, **kwargs):
        captured["url"] = url
        captured["headers"] = headers or {}
        return None

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    await provider.warmup()
    return captured


@pytest.mark.mocked
async def test_warmup_omits_authorization_for_keyless_gateway(monkeypatch) -> None:
    """Keyless gateways (Ollama / vLLM / LM Studio) must not receive a
    ``Bearer EMPTY`` header — some reject an unexpected Authorization header."""
    provider = OpenAICompatibleLLMProvider(
        base_url="http://127.0.0.1:11434/v1", model="llama3.1"
    )

    captured = await _capture_warmup_headers(provider, monkeypatch)

    assert captured["url"] == "http://127.0.0.1:11434/v1/models"
    assert "Authorization" not in captured["headers"]


@pytest.mark.mocked
async def test_warmup_sends_authorization_when_real_key_present(monkeypatch) -> None:
    """When a real bearer is configured, warmup forwards it (matching the TS
    provider) so authenticated gateways accept the prewarm request."""
    provider = OpenAICompatibleLLMProvider(
        api_key="sk-real-key",
        base_url="http://127.0.0.1:9/v1",
        model="m",
    )

    captured = await _capture_warmup_headers(provider, monkeypatch)

    assert captured["headers"].get("Authorization") == "Bearer sk-real-key"
