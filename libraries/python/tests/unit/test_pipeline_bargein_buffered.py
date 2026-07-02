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

    async def test_synthesize_sentence_records_heard_prefix_segment(self) -> None:
        """Each response sentence is stamped on the per-turn playback
        timeline at its first audible chunk."""

        class _StubTTS:
            async def synthesize(self, _text: str):
                yield b"\x00" * 6400  # 200 ms of PCM16 @ 16 kHz

        handler = _make_handler(_make_audio_sender())
        handler._tts = _StubTTS()
        handler._is_speaking = True

        from getpatter.services.pipeline_hooks import PipelineHookExecutor

        ctx = handler._build_hook_context()
        await handler._synthesize_sentence(
            "Frase uno.", PipelineHookExecutor(None), ctx, [True]
        )
        await handler._synthesize_sentence(
            "Frase due.", PipelineHookExecutor(None), ctx, [False]
        )

        # (text, start_s, dur_s): "Frase uno." is finalized to its own 0.2 s
        # playout when "Frase due." stamps; the last sentence keeps dur_s=0.0
        # (its extent resolves from the live playback total).
        assert handler._turn_spoken_segments == [
            ("Frase uno.", 0.0, pytest.approx(0.2)),
            ("Frase due.", pytest.approx(0.2), 0.0),
        ]

    async def test_filler_audio_advances_clock_without_segment(self) -> None:
        """record_segment=False (filler / error fallback) advances the
        playback clock but adds no heard-prefix segment."""

        class _StubTTS:
            async def synthesize(self, _text: str):
                yield b"\x00" * 6400

        handler = _make_handler(_make_audio_sender())
        handler._tts = _StubTTS()
        handler._is_speaking = True

        from getpatter.services.pipeline_hooks import PipelineHookExecutor

        await handler._synthesize_sentence(
            "One moment.",
            PipelineHookExecutor(None),
            handler._build_hook_context(),
            [True],
            record_segment=False,
        )

        assert handler._turn_spoken_segments == []
        assert handler._turn_playback_total_s == pytest.approx(0.2)

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


# ---------------------------------------------------------------------------
# Heard-prefix estimation — what did the caller actually listen to?
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHeardResponsePrefix:
    def test_fully_played_turn_returns_full_text(self) -> None:
        # (a) everything drained out of the carrier → full text, everything
        # heard.
        handler = _make_handler(_make_audio_sender())
        handler._turn_spoken_segments = [
            ("Frase uno.", 0.0, 2.0),
            ("Frase due.", 2.0, 0.0),
        ]
        handler._turn_playback_total_s = 4.0
        handler._playback_buffered_until = 0.0  # long drained

        text, heard_everything = handler._heard_response_prefix()

        assert text == "Frase uno. Frase due."
        assert heard_everything is True

    def test_midsentence_interruption_splits_on_word_boundary(self) -> None:
        # (b) barge-in after 1.5 of 3 sentences: sentence one in full plus the
        # heard fraction of sentence two, cut on a word boundary (never
        # mid-word). heard_everything False.
        handler = _make_handler(_make_audio_sender())
        handler._turn_spoken_segments = [
            ("Sentence one here.", 0.0, 2.0),  # 0..2 s
            ("The second sentence goes here.", 2.0, 2.0),  # 2..4 s (5 words)
            ("Third and final sentence.", 4.0, 0.0),  # 4..6 s
        ]
        handler._turn_playback_total_s = 6.0
        # 3.0 s played → sentence one full, sentence two half (word boundary).
        handler._playback_buffered_until = time.time() + 3.0

        text, heard_everything = handler._heard_response_prefix()

        assert text == "Sentence one here. The second sentence"
        assert heard_everything is False

    def test_sentence_exactly_at_playhead_is_not_yet_heard(self) -> None:
        # A sentence whose start == the play head has produced no sound yet:
        # accurate semantics count it as unsaid (no round-up to whole
        # sentences, unlike the pre-fix behaviour).
        handler = _make_handler(_make_audio_sender())
        handler._turn_spoken_segments = [
            ("Frase uno.", 0.0, 2.0),
            ("Frase due.", 2.0, 2.0),
            ("Frase tre.", 4.0, 0.0),
        ]
        handler._turn_playback_total_s = 6.0
        handler._playback_buffered_until = time.time() + 4.0  # heard = 2.0 s

        text, heard_everything = handler._heard_response_prefix()

        assert text == "Frase uno."
        assert heard_everything is False

    def test_no_segments_returns_none(self) -> None:
        handler = _make_handler(_make_audio_sender())
        assert handler._heard_response_prefix() is None
        assert handler._heard_response_split() is None

    def test_unsaid_remainder_is_exact_complement(self) -> None:
        # (f) the unsaid accessor is the exact complement of the heard prefix:
        # heard + unsaid reconstructs the whole spoken text.
        handler = _make_handler(_make_audio_sender())
        handler._turn_spoken_segments = [
            ("Sentence one here.", 0.0, 2.0),
            ("The second sentence goes here.", 2.0, 2.0),
            ("Third and final sentence.", 4.0, 0.0),
        ]
        handler._turn_playback_total_s = 6.0
        handler._playback_buffered_until = time.time() + 3.0  # heard = 3.0 s

        heard, _heard_everything = handler._heard_response_prefix()
        _h, unsaid, _e = handler._heard_response_split()
        full = (
            "Sentence one here. The second sentence goes here. "
            "Third and final sentence."
        )

        assert heard == "Sentence one here. The second sentence"
        assert unsaid == "goes here. Third and final sentence."
        assert " ".join(p for p in (heard, unsaid) if p) == full


@pytest.mark.unit
@pytest.mark.asyncio
class TestHeardPrefixMarkCursor:
    async def test_twilio_mark_cursor_beats_byte_estimate(self) -> None:
        # (c) a seg_<n> carrier echo advances the mark-confirmed cursor and
        # yields a further-along (more accurate) prefix than the lagging byte
        # estimate.
        handler = _make_handler(_make_audio_sender())
        handler._turn_spoken_segments = [
            ("Uno.", 0.0, 2.0),
            ("Due.", 2.0, 2.0),
            ("Tre.", 4.0, 0.0),
        ]
        handler._turn_playback_total_s = 6.0
        # Byte estimate lags badly: 5.5 s still "buffered" → ~0.5 s "played".
        handler._playback_buffered_until = time.time() + 5.5

        byte_only, _ = handler._heard_response_prefix()
        assert byte_only == ""  # nothing confirmed heard yet

        # The carrier echoes seg_2 (end of the second sentence at 4.0 s).
        handler._seg_mark_end_s = {"seg_1": 2.0, "seg_2": 4.0}
        await handler.on_mark("seg_2")
        assert handler._mark_confirmed_s == 4.0

        text, heard_everything = handler._heard_response_prefix()
        assert text == "Uno. Due."
        assert heard_everything is False

    async def test_synthesize_sentence_emits_segment_mark(self) -> None:
        class _StubTTS:
            async def synthesize(self, _text: str):
                yield b"\x00" * 6400  # 200 ms PCM16 @ 16 kHz

        sender = _make_audio_sender()
        handler = _make_handler(sender)
        handler._tts = _StubTTS()
        handler._is_speaking = True

        from getpatter.services.pipeline_hooks import PipelineHookExecutor

        await handler._synthesize_sentence(
            "Frase uno.",
            PipelineHookExecutor(None),
            handler._build_hook_context(),
            [True],
        )

        sender.send_mark.assert_awaited_with("seg_1")
        assert handler._seg_mark_end_s["seg_1"] == pytest.approx(0.2)

    async def test_seg_mark_does_not_disturb_fm_flow_control(self) -> None:
        # (e) fm_* first-message flow-control still resolves unbroken; a seg_
        # mark only advances the heard cursor and never touches the fm_ FIFO.
        handler = _make_handler(_make_audio_sender())
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[None] = loop.create_future()
        handler._pending_marks = [("fm_1", fut)]
        handler._seg_mark_end_s = {"seg_1": 2.0}

        await handler.on_mark("seg_1")
        assert handler._mark_confirmed_s == 2.0
        assert handler._pending_marks == [("fm_1", fut)]
        assert not fut.done()

        await handler.on_mark("fm_1")
        assert fut.done()
        assert handler._pending_marks == []


# ---------------------------------------------------------------------------
# Post-complete barge-in — rewrite history to the heard prefix
# ---------------------------------------------------------------------------


def _completed_turn_handler(full_text: str) -> PipelineStreamHandler:
    """Handler in the post-complete state: reply recorded in history, carrier
    still playing the buffered tail."""
    handler = _make_handler(_make_audio_sender())
    handler._is_speaking = True
    handler.conversation_history.append(
        {"role": "assistant", "text": full_text, "timestamp": time.time()}
    )
    handler.transcript_entries.append({"role": "assistant", "text": full_text})
    handler._turn_spoken_segments = [
        ("Frase uno.", 0.0, 2.0),  # 0..2 s
        ("Frase due.", 2.0, 2.0),  # 2..4 s
        ("Frase tre.", 4.0, 0.0),  # 4..6 s
    ]
    handler._turn_playback_total_s = 6.0
    # 1.8 s still buffered → heard ≈ 4.2 s: sentence one and two in full,
    # sentence three barely begun (rounds to no words).
    handler._playback_buffered_until = time.time() + 1.8
    return handler


@pytest.mark.unit
@pytest.mark.asyncio
class TestPostCompleteHeardPrefixRewrite:
    async def test_bargein_during_tail_rewrites_history(self) -> None:
        """A barge-in after turn-complete must truncate the recorded reply to
        the heard prefix so a stateful runtime doesn't 'remember saying'
        sentences the caller never heard."""
        full = "Frase uno. Frase due. Frase tre."
        handler = _completed_turn_handler(full)

        await handler._do_cancel_for_barge_in("aspetta")

        expected = "Frase uno. Frase due. [interrupted by caller]"
        assert handler.conversation_history[-1]["text"] == expected
        assert handler.transcript_entries[-1]["text"] == expected

    async def test_no_backlog_no_rewrite(self) -> None:
        full = "Frase uno. Frase due. Frase tre."
        handler = _completed_turn_handler(full)
        handler._playback_buffered_until = 0.0  # everything already played

        await handler._do_cancel_for_barge_in("ok")

        assert handler.conversation_history[-1]["text"] == full

    async def test_in_flight_turn_is_not_rewritten(self) -> None:
        """While a turn is still in flight the streaming path owns the
        marker — the post-complete rewrite must not double-apply."""
        full = "Frase uno. Frase due. Frase tre."
        handler = _completed_turn_handler(full)
        handler._dispatch_task = asyncio.create_task(asyncio.sleep(0.5))

        try:
            await handler._do_cancel_for_barge_in("aspetta")
            assert handler.conversation_history[-1]["text"] == full
        finally:
            handler._dispatch_task.cancel()
            try:
                await handler._dispatch_task
            except asyncio.CancelledError:
                pass


# ---------------------------------------------------------------------------
# Mid-turn barge-in — the marker records only the heard prefix
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestMidTurnHeardPrefixMarker:
    async def test_interrupted_response_truncates_to_heard_prefix(
        self, monkeypatch
    ) -> None:
        """An agent-runtime LLM delivers the full reply at once: every
        sentence is synthesized into the carrier buffer within ms, but the
        caller has only HEARD the first one when the barge-in lands. The
        history marker must record that heard prefix, not the whole reply."""
        monkeypatch.setenv("PATTER_TTS_TAIL_GRACE_MS", "0")
        handler = _make_handler(_make_audio_sender())

        class _BigChunkTTS:
            output_format = "pcm_16000"

            async def synthesize(self, _text: str):
                yield b"\x00" * 64000  # 2 s of PCM16 @ 16 kHz per sentence

        handler._tts = _BigChunkTTS()

        async def _result():
            yield "Frase uno. "
            yield "Frase due. "
            # Both sentences were PUSHED into the carrier buffer within ms, but
            # the caller has only PLAYED sentence one (2.0 s) — the carrier
            # confirmed its seg mark. Pin the mark-confirmed cursor to mirror
            # that (the byte estimate alone would say ~nothing played yet, as
            # nothing has drained in real time).
            handler._mark_confirmed_s = 2.0
            handler._llm_cancel_event.set()
            yield "Frase tre."

        text = await handler._process_streaming_response(_result(), "call-heard")

        assert handler._last_response_interrupted is True
        assert text == "Frase uno. [interrupted by caller]"
