"""Authentic tests for the opt-in spoken LLM-error fallback (pipeline mode).

When the per-turn LLM stream raises (gateway-down / timeout) BEFORE any
assistant text was spoken, and the agent configured a non-empty
``llm_error_message``, the SDK speaks that line through the SAME TTS turn
lifecycle every normal sentence uses (``_synthesize_sentence`` →
``_tts.synthesize`` → ``audio_sender.send_audio``).

Only the external boundary is mocked: the LLM provider's ``stream()`` raising
(the gateway hop) and the TTS byte boundary (``_tts.synthesize`` yielding PCM).
Everything from there inward — the real ``LLMLoop.run`` async generator, the
real ``PipelineStreamHandler._process_streaming_response`` error path, the real
``_synthesize_sentence`` speak primitive, and the real metrics accounting — runs
unmocked. These tests carry ``@pytest.mark.mocked`` because the provider stream
throw is an external-boundary mock.
"""

from __future__ import annotations

from collections import deque
from unittest.mock import AsyncMock, MagicMock

import pytest

from getpatter.exceptions import PatterConnectionError
from getpatter.stream_handler import PipelineStreamHandler

from tests.conftest import make_agent

_FALLBACK = "Sorry, I am having trouble right now."


# ---------------------------------------------------------------------------
# Boundary doubles — the ONLY mocks: the LLM gateway stream and the TTS bytes
# ---------------------------------------------------------------------------


class _RaisingLLMProvider:
    """LLM provider whose ``stream()`` raises before yielding any text.

    Mirrors a gateway-down / timeout: the real inherited provider path raises
    ``PatterConnectionError`` on a non-OK response. This is the single external
    boundary mocked in these tests.
    """

    async def stream(self, messages, tools=None, **_kwargs):
        if False:  # pragma: no cover - make this an async generator
            yield {}
        raise PatterConnectionError("gateway down")


class _PartialTokenThenRaiseLLMProvider:
    """Yields one *partial* token (no sentence boundary), THEN raises.

    The chunker buffers ``"Let me check "`` without producing a complete
    sentence, so ``_synthesize_sentence`` is never called and NO PCM reaches
    the carrier — the caller heard SILENCE. This is the agent-runtime
    (Hermes / OpenClaw) gateway-timeout case: tokens were received but the
    fallback must still fire because nothing was actually spoken.
    """

    async def stream(self, messages, tools=None, **_kwargs):
        yield {"type": "text", "content": "Let me check "}
        raise PatterConnectionError("gateway down mid-stream")


class _SpokenSentenceThenRaiseLLMProvider:
    """Yields a COMPLETE sentence (real TTS audio emitted), THEN raises.

    A full sentence flushes through the chunker, so ``_synthesize_sentence``
    runs and PCM reaches the carrier. The fallback must NOT fire on top —
    the caller already heard speech and a tacked-on apology would double-speak.
    """

    async def stream(self, messages, tools=None, **_kwargs):
        yield {"type": "text", "content": "Hello there. "}
        raise PatterConnectionError("gateway down after a full sentence")


class _FakeTTS:
    """TTS byte boundary — ``synthesize(text)`` yields a couple of PCM chunks.

    Records every text it was asked to synthesize so a test can assert the
    fallback line (and nothing else) was spoken.
    """

    output_format = "pcm_16000"

    def __init__(self) -> None:
        self.synthesized: list[str] = []

    async def synthesize(self, text: str):
        self.synthesized.append(text)
        # Two 16-bit PCM frames of silence — enough to drive a send_audio.
        yield b"\x00\x00" * 80
        yield b"\x00\x00" * 80


def _make_loop(provider) -> object:
    """Build a REAL ``LLMLoop`` wrapping the boundary provider double."""
    from getpatter.services.llm_loop import LLMLoop

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
    return loop


def _make_handler(*, llm_error_message: str | None, tts) -> PipelineStreamHandler:
    audio_sender = AsyncMock()
    # reset_pcm_carry is called synchronously inside _synthesize_sentence.
    audio_sender.reset_pcm_carry = MagicMock()
    handler = PipelineStreamHandler(
        agent=make_agent(llm_error_message=llm_error_message),
        audio_sender=audio_sender,
        call_id="call-llm-err",
        caller="+15551110000",
        callee="+15552220000",
        resolved_prompt="p",
        metrics=None,
        for_twilio=True,
        on_transcript=None,
        conversation_history=deque(maxlen=10),
        transcript_entries=deque(maxlen=10),
    )
    handler.on_message = None
    handler._tts = tts  # type: ignore[assignment]
    handler._is_speaking = True
    return handler


# ---------------------------------------------------------------------------
# Positive: fallback set + stream raises with zero text → line is spoken
# ---------------------------------------------------------------------------


@pytest.mark.mocked
class TestFallbackSpokenOnError:
    async def test_fallback_line_is_synthesized_and_spoken(self) -> None:
        tts = _FakeTTS()
        handler = _make_handler(llm_error_message=_FALLBACK, tts=tts)
        loop = _make_loop(_RaisingLLMProvider())

        result = loop.run("Hi", [], {"call_id": "call-llm-err"})
        await handler._process_streaming_response(result, "call-llm-err")

        # Observable spoken-bytes outcome: the configured line went through the
        # real TTS primitive AND real audio bytes were sent to the carrier.
        assert tts.synthesized == [_FALLBACK]
        handler.audio_sender.send_audio.assert_awaited()


# ---------------------------------------------------------------------------
# Negative / regression: unset field → nothing spoken on error (today's behaviour)
# ---------------------------------------------------------------------------


@pytest.mark.mocked
class TestNoFallbackWhenUnset:
    async def test_no_fallback_synthesized_when_field_is_none(self) -> None:
        tts = _FakeTTS()
        handler = _make_handler(llm_error_message=None, tts=tts)
        loop = _make_loop(_RaisingLLMProvider())

        result = loop.run("Hi", [], {"call_id": "call-llm-err"})
        await handler._process_streaming_response(result, "call-llm-err")

        # Today's behaviour preserved: silence on LLM error, no fallback speech.
        assert tts.synthesized == []
        handler.audio_sender.send_audio.assert_not_awaited()

    async def test_empty_string_is_treated_as_unset(self) -> None:
        tts = _FakeTTS()
        handler = _make_handler(llm_error_message="", tts=tts)
        loop = _make_loop(_RaisingLLMProvider())

        result = loop.run("Hi", [], {"call_id": "call-llm-err"})
        await handler._process_streaming_response(result, "call-llm-err")

        assert tts.synthesized == []
        handler.audio_sender.send_audio.assert_not_awaited()


# ---------------------------------------------------------------------------
# Gate semantics: fallback fires on emitted-audio, not on received-tokens
# ---------------------------------------------------------------------------


@pytest.mark.mocked
class TestFallbackGatedOnEmittedAudio:
    async def test_fallback_fires_when_partial_tokens_produced_no_audio(self) -> None:
        """Partial tokens buffered by the chunker but never synthesized → the
        caller heard SILENCE → the fallback line MUST still be spoken.

        This is the agent-runtime gateway-timeout regression: gating on token
        receipt (the old ``not full_response_parts`` check) wrongly suppressed
        the fallback here even though no PCM ever reached the carrier.
        """
        tts = _FakeTTS()
        handler = _make_handler(llm_error_message=_FALLBACK, tts=tts)
        loop = _make_loop(_PartialTokenThenRaiseLLMProvider())

        result = loop.run("Hi", [], {"call_id": "call-llm-err"})
        await handler._process_streaming_response(result, "call-llm-err")

        # The partial token never produced a sentence, so the ONLY thing
        # synthesized is the fallback line — and real audio was sent.
        assert tts.synthesized == [_FALLBACK]
        handler.audio_sender.send_audio.assert_awaited()

    async def test_fallback_suppressed_after_a_full_sentence_was_spoken(self) -> None:
        """A complete sentence flushed real TTS audio before the raise → the
        caller already heard speech → the fallback must NOT double-speak.
        """
        tts = _FakeTTS()
        handler = _make_handler(llm_error_message=_FALLBACK, tts=tts)
        loop = _make_loop(_SpokenSentenceThenRaiseLLMProvider())

        result = loop.run("Hi", [], {"call_id": "call-llm-err"})
        await handler._process_streaming_response(result, "call-llm-err")

        # The real sentence was spoken; the fallback line was NOT appended.
        assert "Hello there." in tts.synthesized
        assert _FALLBACK not in tts.synthesized


# ---------------------------------------------------------------------------
# Barge-in guard: speaking flipped off before the raise → no fallback
# ---------------------------------------------------------------------------


@pytest.mark.mocked
class TestBargeInSuppressesFallback:
    async def test_fallback_not_spoken_when_not_speaking(self) -> None:
        tts = _FakeTTS()
        handler = _make_handler(llm_error_message=_FALLBACK, tts=tts)

        # Simulate a concurrent barge-in that flipped the floor off right as
        # the stream raises.
        class _FlipThenRaise:
            async def stream(self, messages, tools=None, **_kwargs):
                if False:  # pragma: no cover
                    yield {}
                handler._is_speaking = False
                raise PatterConnectionError("gateway down during barge-in")

        loop = _make_loop(_FlipThenRaise())
        result = loop.run("Hi", [], {"call_id": "call-llm-err"})
        await handler._process_streaming_response(result, "call-llm-err")

        assert tts.synthesized == []
        handler.audio_sender.send_audio.assert_not_awaited()


# ---------------------------------------------------------------------------
# Authenticity invariant: the positive test exercises the REAL speak primitive
# ---------------------------------------------------------------------------


@pytest.mark.mocked
class TestExercisesRealSpeakPrimitive:
    async def test_fails_if_synthesize_sentence_is_not_real(self) -> None:
        tts = _FakeTTS()
        handler = _make_handler(llm_error_message=_FALLBACK, tts=tts)
        loop = _make_loop(_RaisingLLMProvider())

        async def _broken(*_a, **_k):
            raise NotImplementedError

        # Replace the real speak primitive: the fallback's own try/except must
        # swallow the failure (degrade to silence) — so the line is NOT spoken,
        # proving the positive test above depends on the REAL primitive running.
        handler._synthesize_sentence = _broken  # type: ignore[assignment]

        result = loop.run("Hi", [], {"call_id": "call-llm-err"})
        # Must not raise — a TTS/primitive outage on top of an LLM outage
        # degrades to today's silence, not a handler crash.
        await handler._process_streaming_response(result, "call-llm-err")

        assert tts.synthesized == []
