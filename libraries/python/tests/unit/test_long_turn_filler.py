"""Authentic tests for the opt-in long-turn filler (pipeline mode, Feature #8).

When an LLM turn is SLOW (e.g. an agent runtime running tools) and NO audio has
reached the carrier after ``agent.long_turn_message_after_s`` seconds, the SDK
speaks a short filler instead of dead silence — distinct from
``llm_error_message`` (which fires on an ERROR, not on slowness).

Only the external boundary is mocked: the LLM provider's ``stream()`` (its
timing / the gateway hop) and the TTS byte boundary
(``_tts.synthesize`` yielding PCM). Everything inward — the real ``LLMLoop.run``
async generator, the real ``PipelineStreamHandler._process_streaming_response``,
the real filler task scheduling / cancellation, the real ``_synthesize_sentence``
speak primitive — runs unmocked. The filler timeout is set tiny (a few ms) so
the suite stays fast while exercising the real ``asyncio.sleep`` path.

These tests carry ``@pytest.mark.mocked`` because the provider stream is an
external-boundary mock.
"""

from __future__ import annotations

import asyncio
from collections import deque
from unittest.mock import AsyncMock, MagicMock

import pytest

from getpatter.stream_handler import PipelineStreamHandler

from tests.conftest import make_agent

_FILLER = "One moment while I look that up."


# ---------------------------------------------------------------------------
# Boundary doubles — the ONLY mocks: the LLM stream timing and the TTS bytes
# ---------------------------------------------------------------------------


class _SlowThenTextLLMProvider:
    """Sleeps past the filler timeout, THEN yields a complete sentence.

    Models an agent runtime that runs tools for a while before producing its
    first words: the caller hears silence during ``delay_s``, the filler fires,
    and only then the real reply arrives.
    """

    def __init__(self, delay_s: float) -> None:
        self._delay_s = delay_s

    async def stream(self, messages, tools=None, **_kwargs):
        await asyncio.sleep(self._delay_s)
        yield {"type": "text", "content": "Here is your answer. "}


class _FastTextLLMProvider:
    """Yields a complete sentence immediately (no slow gap)."""

    async def stream(self, messages, tools=None, **_kwargs):
        yield {"type": "text", "content": "Quick reply right away. "}


class _FakeTTS:
    """TTS byte boundary — ``synthesize(text)`` yields a couple of PCM chunks.

    Records every text it was asked to synthesize so a test can assert whether
    (and in what order) the filler / the real reply were spoken.
    """

    output_format = "pcm_16000"

    def __init__(self) -> None:
        self.synthesized: list[str] = []

    async def synthesize(self, text: str):
        self.synthesized.append(text)
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
    loop._usage_missing_count = 0
    loop._logged_usage_fallback = False
    return loop


def _make_handler(*, long_turn_message, long_turn_message_after_s, tts):
    audio_sender = AsyncMock()
    audio_sender.reset_pcm_carry = MagicMock()
    overrides: dict = {"long_turn_message": long_turn_message}
    if long_turn_message_after_s is not None:
        overrides["long_turn_message_after_s"] = long_turn_message_after_s
    handler = PipelineStreamHandler(
        agent=make_agent(**overrides),
        audio_sender=audio_sender,
        call_id="call-long-turn",
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
# Positive: slow turn + message set → filler is spoken before the real reply
# ---------------------------------------------------------------------------


@pytest.mark.mocked
class TestFillerSpokenOnSlowTurn:
    async def test_filler_spoken_when_turn_is_slow(self) -> None:
        tts = _FakeTTS()
        handler = _make_handler(
            long_turn_message=_FILLER,
            long_turn_message_after_s=0.02,
            tts=tts,
        )
        # The provider takes 80 ms before its first word; the filler fires at
        # 20 ms — so the caller hears the filler, then the real reply.
        loop = _make_loop(_SlowThenTextLLMProvider(delay_s=0.08))

        result = loop.run("Hi", [], {"call_id": "call-long-turn"})
        await handler._process_streaming_response(result, "call-long-turn")

        # The filler was synthesized (and reached the carrier) FIRST, then the
        # real reply followed — exactly one filler, no double-speak.
        assert _FILLER in tts.synthesized
        assert tts.synthesized.index(_FILLER) == 0
        assert "Here is your answer." in tts.synthesized
        assert tts.synthesized.count(_FILLER) == 1
        handler.audio_sender.send_audio.assert_awaited()


# ---------------------------------------------------------------------------
# Negative: fast turn → filler must NOT fire (no race / no double-speak)
# ---------------------------------------------------------------------------


@pytest.mark.mocked
class TestFillerNotSpokenOnFastTurn:
    async def test_filler_not_spoken_when_audio_starts_quickly(self) -> None:
        tts = _FakeTTS()
        handler = _make_handler(
            long_turn_message=_FILLER,
            long_turn_message_after_s=0.5,  # well beyond the fast reply
            tts=tts,
        )
        loop = _make_loop(_FastTextLLMProvider())

        result = loop.run("Hi", [], {"call_id": "call-long-turn"})
        await handler._process_streaming_response(result, "call-long-turn")

        # Real audio started immediately; the filler is cancelled before firing.
        assert _FILLER not in tts.synthesized
        assert "Quick reply right away." in tts.synthesized

    async def test_no_orphaned_filler_task_after_fast_turn(self) -> None:
        """The filler task must be cleanly cancelled — no pending filler task
        lingers past the turn (the cancellation path awaits/suppresses
        CancelledError). Captures the actual task handle the handler created."""
        tts = _FakeTTS()
        handler = _make_handler(
            long_turn_message=_FILLER,
            long_turn_message_after_s=0.5,
            tts=tts,
        )
        loop = _make_loop(_FastTextLLMProvider())

        created: list = []
        real_schedule = handler._schedule_long_turn_filler

        def _capture(*args, **kwargs):
            task = real_schedule(*args, **kwargs)
            if task is not None:
                created.append(task)
            return task

        handler._schedule_long_turn_filler = _capture  # type: ignore[assignment]

        result = loop.run("Hi", [], {"call_id": "call-long-turn"})
        await handler._process_streaming_response(result, "call-long-turn")
        # Yield once so any cancelled task fully tears down.
        await asyncio.sleep(0)

        # The filler task was created and is now finished (cancelled cleanly),
        # not left pending past the turn.
        assert len(created) == 1
        assert created[0].done()
        assert created[0].cancelled()


# ---------------------------------------------------------------------------
# Regression: feature OFF by default → behaviour unchanged
# ---------------------------------------------------------------------------


@pytest.mark.mocked
class TestFillerOffByDefault:
    async def test_unset_message_speaks_nothing_extra(self) -> None:
        tts = _FakeTTS()
        handler = _make_handler(
            long_turn_message=None,  # default — feature OFF
            long_turn_message_after_s=None,
            tts=tts,
        )
        # Even on a slow turn, with the message unset nothing extra is spoken.
        loop = _make_loop(_SlowThenTextLLMProvider(delay_s=0.05))

        result = loop.run("Hi", [], {"call_id": "call-long-turn"})
        await handler._process_streaming_response(result, "call-long-turn")

        # Only the real reply — no filler ever synthesized.
        assert tts.synthesized == ["Here is your answer."]


# ---------------------------------------------------------------------------
# Barge-in guard: floor flipped off during the slow gap → filler stays silent
# ---------------------------------------------------------------------------


@pytest.mark.mocked
class TestFillerSuppressedByBargeIn:
    async def test_filler_not_spoken_when_floor_flips_off_before_firing(self) -> None:
        tts = _FakeTTS()
        handler = _make_handler(
            long_turn_message=_FILLER,
            long_turn_message_after_s=0.02,
            tts=tts,
        )

        class _SlowFlipThenText:
            async def stream(self, messages, tools=None, **_kwargs):
                # Concurrent barge-in flips the floor off before the filler
                # timeout elapses — the filler must observe ``_is_speaking`` is
                # False and stay silent.
                handler._is_speaking = False
                await asyncio.sleep(0.08)
                handler._is_speaking = True  # restored for the (real) reply path
                yield {"type": "text", "content": "Late reply. "}

        loop = _make_loop(_SlowFlipThenText())
        result = loop.run("Hi", [], {"call_id": "call-long-turn"})
        await handler._process_streaming_response(result, "call-long-turn")

        assert _FILLER not in tts.synthesized


# ---------------------------------------------------------------------------
# Authenticity invariant: the positive test exercises the REAL speak primitive
# ---------------------------------------------------------------------------


@pytest.mark.mocked
class TestExercisesRealSpeakPrimitive:
    async def test_fails_if_synthesize_sentence_is_not_real(self) -> None:
        tts = _FakeTTS()
        handler = _make_handler(
            long_turn_message=_FILLER,
            long_turn_message_after_s=0.02,
            tts=tts,
        )
        loop = _make_loop(_SlowThenTextLLMProvider(delay_s=0.08))

        filler_attempts: list[str] = []
        real_synth = handler._synthesize_sentence

        async def _broken_for_filler(sentence, *args, **kwargs):
            # The FILLER's speak primitive is broken (its own try/except must
            # swallow the failure → no filler PCM reaches the carrier). The real
            # reply still routes to the genuine primitive, so the turn completes.
            if sentence == _FILLER:
                filler_attempts.append(sentence)
                raise NotImplementedError
            return await real_synth(sentence, *args, **kwargs)

        handler._synthesize_sentence = _broken_for_filler  # type: ignore[assignment]

        result = loop.run("Hi", [], {"call_id": "call-long-turn"})
        # Must not raise — a filler-primitive outage degrades to silence, not a
        # handler crash, and the real reply still plays.
        await handler._process_streaming_response(result, "call-long-turn")

        # The filler attempted to speak through the (now-broken) primitive but
        # its bytes never reached the carrier — proving the positive test above
        # depends on the REAL primitive running, not a mock.
        assert filler_attempts == [_FILLER]
        assert _FILLER not in tts.synthesized
        # The real reply still played (broken filler never blocked the turn).
        assert "Here is your answer." in tts.synthesized
