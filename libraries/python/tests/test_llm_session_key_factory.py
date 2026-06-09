"""Tests for the per-call session-key factory (Feature #7).

A ``session_key_factory`` derives the memory-scope header value per call from a
:class:`SessionContext` (carrying ``caller`` + its non-reversible
:func:`hash_caller`). The Hermes convenience ``session_key_from="caller_hash"``
installs a default factory that scopes durable memory per caller WITHOUT the raw
number ever reaching the wire.

Real code throughout — the only mocked surface is the paid external boundary
(``chat.completions.create``), tagged ``@pytest.mark.mocked``. The factory
resolution, the SessionContext construction, and the caller threading through
the REAL ``LLMLoop`` are all exercised against live code.
"""

from __future__ import annotations

import pytest

from getpatter.llm import hermes
from getpatter.llm.openai_compatible import OpenAICompatibleLLMProvider
from getpatter.models import SessionContext, hash_caller
from getpatter.services.llm_loop import LLMLoop, _stream_accepted_context_kwargs


# ---------------------------------------------------------------------------
# hash_caller — stable, non-reversible, never the raw number
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_hash_caller_is_stable_and_not_the_raw_number() -> None:
    number = "+15555550100"
    h1 = hash_caller(number)
    h2 = hash_caller(number)
    # Deterministic across calls.
    assert h1 == h2
    # 16 hex chars (64-bit truncation), and NOT the raw number.
    assert len(h1) == 16
    assert all(c in "0123456789abcdef" for c in h1)
    assert number not in h1
    assert h1 != number


@pytest.mark.unit
def test_hash_caller_distinguishes_different_callers() -> None:
    assert hash_caller("+15555550100") != hash_caller("+15555550101")


@pytest.mark.unit
def test_hash_caller_none_or_empty_returns_none() -> None:
    assert hash_caller(None) is None
    assert hash_caller("") is None


@pytest.mark.unit
def test_session_context_defaults_are_all_none() -> None:
    ctx = SessionContext()
    assert ctx.call_id is None
    assert ctx.caller is None
    assert ctx.callee is None
    assert ctx.caller_hash is None
    # Frozen (immutable public config).
    with pytest.raises(Exception):
        ctx.caller = "x"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Factory precedence on the generic provider
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_factory_overrides_static_session_key_and_sees_caller_hash() -> None:
    seen: dict = {}

    def factory(ctx: SessionContext) -> str:
        seen["ctx"] = ctx
        return f"scope-{ctx.caller_hash}"

    provider = OpenAICompatibleLLMProvider(
        base_url="http://127.0.0.1:9/v1",
        model="m",
        session_key_header="X-Mem",
        session_key="static-key",  # must be overridden by the factory
        session_key_factory=factory,
    )
    kwargs = provider._build_completion_kwargs(
        [{"role": "user", "content": "hi"}],
        None,
        call_id="c1",
        caller="+15555550100",
        callee="+15555550101",
    )
    expected_hash = hash_caller("+15555550100")
    assert kwargs["extra_headers"]["X-Mem"] == f"scope-{expected_hash}"
    # The factory saw the full SessionContext, including the raw caller and the
    # callee — but the EMITTED value carries only the hash.
    ctx = seen["ctx"]
    assert ctx.call_id == "c1"
    assert ctx.caller == "+15555550100"
    assert ctx.callee == "+15555550101"
    assert ctx.caller_hash == expected_hash


@pytest.mark.unit
def test_factory_returning_none_omits_the_header() -> None:
    provider = OpenAICompatibleLLMProvider(
        base_url="http://127.0.0.1:9/v1",
        model="m",
        session_key_header="X-Mem",
        session_key="static-key",
        session_key_factory=lambda ctx: None,
    )
    kwargs = provider._build_completion_kwargs(
        [{"role": "user", "content": "hi"}], None, call_id="c1", caller="+15555550100"
    )
    # Factory returned falsy => header omitted entirely (no extra_headers at all
    # here, since nothing else is configured).
    assert "extra_headers" not in kwargs


@pytest.mark.unit
def test_static_session_key_used_when_no_factory() -> None:
    provider = OpenAICompatibleLLMProvider(
        base_url="http://127.0.0.1:9/v1",
        model="m",
        session_key_header="X-Mem",
        session_key="static-key",
    )
    kwargs = provider._build_completion_kwargs(
        [{"role": "user", "content": "hi"}], None, call_id="c1", caller="+15555550100"
    )
    assert kwargs["extra_headers"] == {"X-Mem": "static-key"}


@pytest.mark.unit
def test_factory_fires_even_without_call_id() -> None:
    """The memory-scope header is per-call-independent: a factory keying off the
    caller hash alone produces a header even with no call id."""
    provider = OpenAICompatibleLLMProvider(
        base_url="http://127.0.0.1:9/v1",
        model="m",
        session_key_header="X-Mem",
        session_key_factory=lambda ctx: f"caller-{ctx.caller_hash}",
    )
    kwargs = provider._build_completion_kwargs(
        [{"role": "user", "content": "hi"}], None, call_id=None, caller="+15555550100"
    )
    assert kwargs["extra_headers"]["X-Mem"] == f"caller-{hash_caller('+15555550100')}"


# ---------------------------------------------------------------------------
# Hermes convenience: session_key_from="caller_hash"
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_hermes_session_key_from_installs_caller_hash_factory() -> None:
    llm = hermes.LLM(session_key_from="caller_hash")
    kwargs = llm._build_completion_kwargs(
        [{"role": "user", "content": "hi"}],
        None,
        call_id="hid-1",
        caller="+15555550100",
    )
    expected = f"patter-caller-{hash_caller('+15555550100')}"
    assert kwargs["extra_headers"]["X-Hermes-Session-Key"] == expected
    # Per-call session id still flows alongside the memory scope.
    assert kwargs["extra_headers"]["X-Hermes-Session-Id"] == "patter-call-hid-1"


@pytest.mark.unit
def test_hermes_session_key_from_omits_header_without_caller() -> None:
    llm = hermes.LLM(session_key_from="caller_hash")
    kwargs = llm._build_completion_kwargs(
        [{"role": "user", "content": "hi"}], None, call_id="hid-1", caller=None
    )
    # No caller => no caller_hash => the default factory returns None => header
    # omitted. The per-call session-id header is still present.
    assert "X-Hermes-Session-Key" not in kwargs["extra_headers"]
    assert kwargs["extra_headers"]["X-Hermes-Session-Id"] == "patter-call-hid-1"


@pytest.mark.unit
def test_hermes_explicit_factory_wins_over_session_key_from() -> None:
    llm = hermes.LLM(
        session_key_from="caller_hash",
        session_key_factory=lambda ctx: "custom-scope",
    )
    kwargs = llm._build_completion_kwargs(
        [{"role": "user", "content": "hi"}], None, call_id="hid-1", caller="+15555550100"
    )
    assert kwargs["extra_headers"]["X-Hermes-Session-Key"] == "custom-scope"


@pytest.mark.unit
def test_hermes_rejects_unknown_session_key_from() -> None:
    with pytest.raises(ValueError, match="caller_hash"):
        hermes.LLM(session_key_from="something-else")


# ---------------------------------------------------------------------------
# Caller threads through the REAL LLMLoop into the provider's stream()
# ---------------------------------------------------------------------------


class _CallerRecordingProvider:
    """Records the caller/callee/call_id it was streamed with."""

    def __init__(self) -> None:
        self.seen: dict = {}

    async def stream(
        self, messages, tools=None, *, cancel_event=None, call_id=None, caller=None, callee=None
    ):
        self.seen = {"call_id": call_id, "caller": caller, "callee": callee}
        yield {"type": "text", "content": "ok"}


class _CallIdOnlyProvider:
    """Older provider that only declares call_id — must NOT receive caller."""

    def __init__(self) -> None:
        self.seen_kwargs: object = "<<unset>>"

    async def stream(self, messages, tools=None, *, cancel_event=None, call_id=None):
        self.seen_kwargs = call_id
        yield {"type": "text", "content": "ok"}


def _make_loop(provider) -> LLMLoop:
    loop = LLMLoop.__new__(LLMLoop)
    loop._provider = provider
    loop._system_prompt = "You are a test assistant."
    loop._tools = None
    loop._tool_executor = None
    loop._metrics = None
    loop._event_bus = None
    loop._model = "fake-model"
    loop._provider_name = "fake"
    loop._openai_tools = None
    loop._tool_map = {}
    loop._on_tool_call = None
    loop._usage_missing_count = 0
    loop._logged_usage_fallback = False
    return loop


@pytest.mark.unit
async def test_caller_callee_thread_through_loop_into_provider() -> None:
    provider = _CallerRecordingProvider()
    loop = _make_loop(provider)
    async for _ in loop.run(
        "Hi", [], {"call_id": "c9", "caller": "+15555550100", "callee": "+15555550101"}
    ):
        pass
    assert provider.seen == {
        "call_id": "c9",
        "caller": "+15555550100",
        "callee": "+15555550101",
    }


@pytest.mark.unit
def test_signature_guard_classifies_caller_aware_provider() -> None:
    accepted = _stream_accepted_context_kwargs(_CallerRecordingProvider())
    assert accepted == frozenset({"call_id", "caller", "callee"})
    # An older call_id-only provider is not handed caller/callee.
    assert _stream_accepted_context_kwargs(_CallIdOnlyProvider()) == frozenset(
        {"call_id"}
    )


@pytest.mark.unit
async def test_call_id_only_provider_never_receives_caller() -> None:
    """A provider that declares only call_id must keep working when the loop has
    caller/callee in context — it gets call_id only, never the new kwargs."""
    provider = _CallIdOnlyProvider()
    loop = _make_loop(provider)
    async for _ in loop.run(
        "Hi", [], {"call_id": "c9", "caller": "+15555550100", "callee": "+15555550101"}
    ):
        pass
    assert provider.seen_kwargs == "c9"


# ---------------------------------------------------------------------------
# Wire-level — mocks ONLY the paid boundary (chat.completions.create).
# ---------------------------------------------------------------------------


class _Choice:
    def __init__(self, content) -> None:
        self.delta = type("D", (), {"content": content, "tool_calls": None})()


class _Chunk:
    def __init__(self, content) -> None:
        self.choices = [_Choice(content)]
        self.usage = None


class _FakeStream:
    def __init__(self, chunks) -> None:
        self._chunks = chunks

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for chunk in self._chunks:
            yield chunk

    async def close(self) -> None:  # pragma: no cover - not exercised
        pass


@pytest.mark.mocked
async def test_hermes_caller_hash_reaches_the_wire() -> None:
    """End-to-end on the wire: Hermes(session_key_from='caller_hash') emits
    X-Hermes-Session-Key=patter-caller-<hash> where <hash>=hash_caller(caller),
    and the raw caller is NEVER in the header value."""
    llm = hermes.LLM(session_key_from="caller_hash")
    captured: dict = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return _FakeStream([_Chunk("ok")])

    llm._client.chat.completions.create = fake_create

    caller = "+15555550100"
    async for _ in llm.stream(
        [{"role": "user", "content": "hi"}], None, call_id="hid-1", caller=caller
    ):
        pass

    headers = captured["extra_headers"]
    expected = f"patter-caller-{hash_caller(caller)}"
    assert headers["X-Hermes-Session-Key"] == expected
    # The raw number is never on the wire in the memory-scope header.
    assert caller not in headers["X-Hermes-Session-Key"]


@pytest.mark.mocked
async def test_custom_factory_overrides_static_on_the_wire() -> None:
    provider = OpenAICompatibleLLMProvider(
        base_url="http://127.0.0.1:9/v1",
        model="m",
        session_key_header="X-Mem",
        session_key="static-key",
        session_key_factory=lambda ctx: f"dyn-{ctx.caller_hash}",
    )
    captured: dict = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return _FakeStream([_Chunk("ok")])

    provider._client.chat.completions.create = fake_create

    async for _ in provider.stream(
        [{"role": "user", "content": "hi"}], None, call_id="c1", caller="+15555550100"
    ):
        pass

    assert captured["extra_headers"]["X-Mem"] == f"dyn-{hash_caller('+15555550100')}"
