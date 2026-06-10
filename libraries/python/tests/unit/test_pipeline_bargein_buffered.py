"""Unit tests for barge-in while the carrier still plays buffered audio.

The pipeline pushes TTS audio to the carrier as fast as the provider
synthesizes it; the carrier buffers and plays at realtime. With an
agent-runtime LLM (Hermes / OpenClaw) the whole — often long — reply
arrives at once, so the SDK finishes *pushing* tens of seconds before the
caller finishes *hearing*. The handler must keep ``_is_speaking=True``
(with ``_tail_grace_active=False``) for that whole audible backlog so a
barge-in still takes the cancel path (``send_clear`` drops the carrier
buffer) instead of being mis-read as a calm next turn — previously the
fixed 1.5 s grace expired mid-reply and "the agent detected the barge-in
but kept talking".

State estimation lives in ``_track_outbound_playback`` /
``_playback_buffered_until``; the two-phase wait lives in
``_end_speaking_with_grace``.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import AsyncIterator, Iterable
from unittest.mock import AsyncMock

import pytest

from getpatter.providers.base import Transcript
from getpatter.stream_handler import PipelineStreamHandler

from tests.conftest import make_agent


class _StubSTT:
    def __init__(self, transcripts: Iterable[Transcript]) -> None:
        self._transcripts = list(transcripts)

    async def receive_transcripts(self) -> AsyncIterator[Transcript]:
        for t in self._transcripts:
            yield t
        await asyncio.sleep(0)


def _make_handler(audio_sender: AsyncMock) -> PipelineStreamHandler:
    handler = PipelineStreamHandler(
        agent=make_agent(),
        audio_sender=audio_sender,
        call_id="call-buffered",
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
    handler._llm_loop = None
    return handler


def _make_audio_sender(*, mulaw_native: bool = False) -> AsyncMock:
    sender = AsyncMock()
    # AsyncMock auto-creates truthy attributes — pin the format flag so
    # ``_track_outbound_playback`` sees the real default (PCM16 @ 16 kHz).
    sender._input_is_mulaw_8k = mulaw_native
    return sender


# ---------------------------------------------------------------------------
# _track_outbound_playback — cursor math
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTrackOutboundPlayback:
    def test_pcm16_16k_chunk_advances_cursor_by_chunk_duration(self) -> None:
        handler = _make_handler(_make_audio_sender())
        before = time.time()
        handler._track_outbound_playback(3200)  # 100 ms at 32 bytes/ms
        assert handler._playback_buffered_until == pytest.approx(before + 0.1, abs=0.05)

    def test_mulaw_8k_native_chunk_uses_8_bytes_per_ms(self) -> None:
        handler = _make_handler(_make_audio_sender(mulaw_native=True))
        before = time.time()
        handler._track_outbound_playback(800)  # 100 ms at 8 bytes/ms
        assert handler._playback_buffered_until == pytest.approx(before + 0.1, abs=0.05)

    def test_back_to_back_chunks_accumulate(self) -> None:
        handler = _make_handler(_make_audio_sender())
        before = time.time()
        handler._track_outbound_playback(3200)
        handler._track_outbound_playback(3200)
        assert handler._playback_buffered_until == pytest.approx(before + 0.2, abs=0.05)

    def test_cursor_rebases_to_now_after_idle_gap(self) -> None:
        handler = _make_handler(_make_audio_sender())
        handler._playback_buffered_until = time.time() - 10.0  # long drained
        before = time.time()
        handler._track_outbound_playback(3200)
        assert handler._playback_buffered_until == pytest.approx(before + 0.1, abs=0.05)

    def test_empty_chunk_is_a_no_op(self) -> None:
        handler = _make_handler(_make_audio_sender())
        handler._track_outbound_playback(0)
        assert handler._playback_buffered_until == 0.0


# ---------------------------------------------------------------------------
# _end_speaking_with_grace — two-phase wait
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestBufferedBacklogHoldsSpeaking:
    async def test_backlog_keeps_speaking_armed_not_tail_grace(
        self, monkeypatch
    ) -> None:
        """While the carrier plays buffered audio the agent is still
        speaking — NOT in the tail-grace window — so barge-in stays armed."""
        monkeypatch.setenv("PATTER_TTS_TAIL_GRACE_MS", "50")
        handler = _make_handler(_make_audio_sender())
        handler._is_speaking = True
        handler._playback_buffered_until = time.time() + 0.5

        await handler._end_speaking_with_grace()
        await asyncio.sleep(0.1)  # well inside the backlog window

        assert handler._is_speaking is True
        assert handler._tail_grace_active is False
        handler._clear_grace_task()

    async def test_backlog_drains_then_tail_grace_then_flip(self, monkeypatch) -> None:
        monkeypatch.setenv("PATTER_TTS_TAIL_GRACE_MS", "50")
        handler = _make_handler(_make_audio_sender())
        handler._is_speaking = True
        handler._playback_buffered_until = time.time() + 0.15

        await handler._end_speaking_with_grace()
        await asyncio.sleep(0.4)  # backlog (150 ms) + grace (50 ms) + margin

        assert handler._is_speaking is False
        assert handler._tail_grace_active is False

    async def test_no_backlog_starts_tail_grace_immediately(self, monkeypatch) -> None:
        """Token-paced LLMs (no carrier backlog) keep today's behaviour."""
        monkeypatch.setenv("PATTER_TTS_TAIL_GRACE_MS", "50")
        handler = _make_handler(_make_audio_sender())
        handler._is_speaking = True
        assert handler._playback_buffered_until == 0.0

        await handler._end_speaking_with_grace()

        assert handler._tail_grace_active is True
        await asyncio.sleep(0.15)
        assert handler._is_speaking is False


# ---------------------------------------------------------------------------
# Barge-in during the buffered backlog — the Hermes/OpenClaw regression
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestBargeInDuringBufferedBacklog:
    async def test_transcript_during_backlog_cancels_and_clears(
        self, monkeypatch
    ) -> None:
        """A transcript while the carrier still plays buffered audio must run
        the FULL cancel path: flip ``_is_speaking`` and ``send_clear`` the
        carrier so the buffered reply actually stops."""
        monkeypatch.setenv("PATTER_TTS_TAIL_GRACE_MS", "50")
        audio_sender = _make_audio_sender()
        handler = _make_handler(audio_sender)
        handler._stt = _StubSTT(
            [Transcript(text="aspetta", is_final=False, confidence=0.5)]
        )
        handler._is_speaking = True
        # Turn finished pushing; carrier still has seconds of audio queued.
        handler._playback_buffered_until = time.time() + 5.0
        await handler._end_speaking_with_grace()

        assert handler._is_speaking is True  # backlog holds the floor
        await asyncio.wait_for(handler._stt_loop(), timeout=2.0)

        audio_sender.send_clear.assert_awaited_once()
        assert handler._is_speaking is False
        assert handler._playback_buffered_until == 0.0

    async def test_cancel_resets_cursor_and_grace_task(self) -> None:
        handler = _make_handler(_make_audio_sender())
        handler._is_speaking = True
        handler._playback_buffered_until = time.time() + 5.0

        await handler._do_cancel_for_barge_in("stop")

        assert handler._playback_buffered_until == 0.0
        assert handler._grace_task is None

    async def test_synthesize_sentence_tracks_pushed_audio(self) -> None:
        """The pipeline TTS path must advance the playback cursor for every
        chunk it pushes to the carrier."""

        class _StubTTS:
            async def synthesize(self, _text: str):
                yield b"\x00" * 6400  # 200 ms of PCM16 @ 16 kHz

        handler = _make_handler(_make_audio_sender())
        handler._tts = _StubTTS()
        handler._is_speaking = True

        from getpatter.services.pipeline_hooks import PipelineHookExecutor

        before = time.time()
        ok = await handler._synthesize_sentence(
            "ciao", PipelineHookExecutor(None), handler._build_hook_context(), [True]
        )

        assert ok is True
        assert handler._playback_buffered_until == pytest.approx(before + 0.2, abs=0.1)
