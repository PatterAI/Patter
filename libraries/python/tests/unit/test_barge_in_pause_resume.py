"""Pause-and-resume false-interruption handling (``barge_in_mode="pause_resume"``).

LiveKit-style three-way state machine on top of the existing barge-in
machinery:

* PAUSE — VAD ``speech_start`` while the agent speaks gates the send loops
  on ``_output_paused`` and clears the carrier buffer. Nothing is
  cancelled: the LLM stream keeps buffering sentences (bounded) and the
  TTS stream keeps queueing audio into per-sentence retention entries
  (bounded).
* KILL — a committed final transcript (non-echo / non-hallucination /
  non-duplicate) within ``barge_in_confirm_ms`` runs the full cancel path
  (``_do_cancel_for_barge_in``) and discards the paused buffers.
* RESUME — the window expires with no confirming transcript: the
  cleared-but-unheard tail is re-sent from the retained audio at SENTENCE
  granularity (first sentence not fully played) and the event is recorded
  as a false interruption (``record_overlap_end(was_interruption=False)``
  — the backchannel path, never an interruption count).

Fixtures mirror ``test_barge_in_two_stage.py``: handlers are built via
``object.__new__`` with only the fields the path under test touches.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from getpatter.providers.base import Transcript
from getpatter.stream_handler import PipelineStreamHandler


def _make_handler(
    *,
    mode: str = "pause_resume",
    confirm_s: float = 0.05,
) -> PipelineStreamHandler:
    """Minimally-wired handler for the pause-resume decision path."""
    h = object.__new__(PipelineStreamHandler)
    h._is_speaking = True
    h._aec = None
    h._speaking_started_at = time.time() - 1.0  # past the no-AEC gate
    h._first_audio_sent_at = time.time() - 1.0
    h._speaking_generation = 0
    h._last_cancel_at = None
    h._tail_grace_active = False
    h._grace_task = None
    h._llm_cancel_event = asyncio.Event()
    h.metrics = MagicMock()
    h.call_id = "test-call"
    h.audio_sender = MagicMock()
    h.audio_sender.send_clear = AsyncMock()
    h.audio_sender.send_audio = AsyncMock()
    h.audio_sender.reset_pcm_carry = MagicMock()
    h.audio_sender._input_is_mulaw_8k = False  # PCM16 16 kHz: 32 kB/s
    h._inbound_audio_ring = []
    h._stt = None
    h._tts = None
    h._event_bus = None
    h._barge_in_strategies = ()
    h._barge_in_confirm_s = confirm_s
    h._barge_in_pending_since = None
    h._barge_in_pending_task = None
    h._barge_in_mode = mode
    h._output_paused = False
    h._pause_decision_event = None
    h._paused_sentences = []
    h._turn_sentence_audio = []
    h._pause_retained_bytes = 0
    h._pause_resume_overflowed = False
    h._pause_resume_index = 0
    h._pending_marks = []
    h._dispatch_task = None
    h._last_commit_text = ""
    h._last_commit_at = 0.0
    h._turn_playback_total_s = 0.0
    h._turn_spoken_segments = []
    h._playback_buffered_until = 0.0
    h.conversation_history = []
    h.transcript_entries = []
    return h


async def _wait_until(predicate, deadline_s: float = 2.0) -> None:
    """Bounded poll so CI scheduling jitter cannot flake the fake-clock
    style tests (same pattern as test_barge_in_two_stage)."""
    deadline = asyncio.get_event_loop().time() + deadline_s
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)


def _seed_partially_heard_turn(h: PipelineStreamHandler) -> None:
    """Three sentences pushed; sentence one fully played, sentence two
    mid-playback, sentence three still entirely in the carrier buffer."""
    h._turn_spoken_segments = [
        ("Frase uno.", 0.0),
        ("Frase due.", 2.0),
        ("Frase tre.", 4.0),
    ]
    h._turn_playback_total_s = 6.0
    # 3.5 s still buffered → heard = 2.5 s: inside sentence two.
    h._playback_buffered_until = time.time() + 3.5
    h._turn_sentence_audio = [
        {"text": "Frase uno.", "chunks": [b"\x01" * 320], "sent": 1},
        {"text": "Frase due.", "chunks": [b"\x02" * 320, b"\x03" * 320], "sent": 2},
        {"text": "Frase tre.", "chunks": [b"\x04" * 320], "sent": 1},
    ]
    h._pause_retained_bytes = 4 * 320


# ---------------------------------------------------------------------------
# PAUSE — speech_start gates output without cancelling anything
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPauseOnSpeechStart:
    async def test_pause_clears_carrier_but_cancels_nothing(self) -> None:
        h = _make_handler()
        _seed_partially_heard_turn(h)

        await h._start_pause_resume()

        assert h._output_paused is True
        assert h._is_speaking is True, "pause must NOT take the floor"
        assert not h._llm_cancel_event.is_set(), "LLM stream must stay alive"
        h.audio_sender.send_clear.assert_awaited_once()
        h.metrics.record_overlap_start.assert_called_once()
        h.metrics.record_bargein_detected.assert_not_called()
        h.metrics.record_turn_interrupted.assert_not_called()
        assert h._barge_in_pending_since is not None
        assert h._barge_in_pending_task is not None
        h._clear_pending_barge_in()

    async def test_pause_freezes_playback_cursor_at_heard_offset(self) -> None:
        h = _make_handler()
        _seed_partially_heard_turn(h)

        await h._start_pause_resume()

        # heard = total (6.0) - backlog (3.5) = 2.5 s; the cleared backlog
        # is void so the cursor snaps to "nothing pending".
        assert h._turn_playback_total_s == pytest.approx(2.5, abs=0.1)
        assert h._playback_buffered_until == 0.0
        # Resume offset: sentence two (start 2.0, end 4.0 > heard 2.5) is
        # the first not FULLY played → index 1.
        assert h._pause_resume_index == 1
        h._clear_pending_barge_in()

    async def test_pause_is_idempotent(self) -> None:
        h = _make_handler(confirm_s=10)
        await h._start_pause_resume()
        first_since = h._barge_in_pending_since
        first_task = h._barge_in_pending_task

        await h._start_pause_resume()

        assert h._barge_in_pending_since == first_since
        assert h._barge_in_pending_task is first_task
        h.metrics.record_overlap_start.assert_called_once()
        h.audio_sender.send_clear.assert_awaited_once()
        h._clear_pending_barge_in()

    async def test_should_pause_requires_mode_and_resumable_state(self) -> None:
        h = _make_handler(mode="cancel")
        assert h._should_pause_for_barge_in() is False

        h = _make_handler()
        # No dispatch in flight, no retained audio (e.g. the firstMessage
        # paced sender) → fall back to today's immediate cancel.
        assert h._should_pause_for_barge_in() is False

        h._turn_sentence_audio = [{"text": "x", "chunks": [b"a"], "sent": 1}]
        assert h._should_pause_for_barge_in() is True

        h = _make_handler()
        h._dispatch_task = asyncio.create_task(asyncio.sleep(0.2))
        try:
            assert h._should_pause_for_barge_in() is True
        finally:
            h._dispatch_task.cancel()
            try:
                await h._dispatch_task
            except asyncio.CancelledError:
                pass

        h = _make_handler()
        h._pause_resume_overflowed = True
        h._turn_sentence_audio = [{"text": "x", "chunks": [b"a"], "sent": 1}]
        assert h._should_pause_for_barge_in() is False


# ---------------------------------------------------------------------------
# While paused — synthesis queues, sentences buffer
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPausedBuffering:
    async def test_synthesize_sentence_queues_audio_while_paused(self) -> None:
        """The TTS provider stream keeps producing while paused; chunks go
        to the retention entry, never to the carrier."""

        class _StubTTS:
            async def synthesize(self, _text: str):
                yield b"\x00" * 640
                yield b"\x01" * 640

        from getpatter.services.pipeline_hooks import PipelineHookExecutor

        h = _make_handler()
        h._tts = _StubTTS()
        h._output_paused = True
        h.agent = MagicMock()
        h.agent.text_transforms = None
        h.agent.hooks = None
        h.caller = "+1"
        h.callee = "+2"

        ok = await h._synthesize_sentence(
            "Frase uno.", PipelineHookExecutor(None), h._build_hook_context(), [True]
        )

        assert ok is True, "a paused sentence is queued, not interrupted"
        h.audio_sender.send_audio.assert_not_awaited()
        assert len(h._turn_sentence_audio) == 1
        entry = h._turn_sentence_audio[0]
        assert entry["text"] == "Frase uno."
        assert entry["chunks"] == [b"\x00" * 640, b"\x01" * 640]
        assert entry["sent"] == 0
        # Nothing was sent → no heard-prefix segment and no cursor motion.
        assert h._turn_spoken_segments == []
        assert h._turn_playback_total_s == 0.0

    async def test_sentences_buffer_as_text_while_paused(self) -> None:
        h = _make_handler()
        h._output_paused = True

        assert await h._buffer_sentence_if_paused("Frase uno.") is True
        assert await h._buffer_sentence_if_paused("Frase due.") is True
        assert h._paused_sentences == ["Frase uno.", "Frase due."]
        assert h._is_speaking is True

        h._output_paused = False
        assert await h._buffer_sentence_if_paused("Frase tre.") is False
        assert h._release_paused_sentences() == ["Frase uno.", "Frase due."]
        assert h._paused_sentences == []

    async def test_sentence_buffer_overflow_degrades_to_full_cancel(self) -> None:
        h = _make_handler()
        h._output_paused = True
        h._paused_sentences = ["s"] * h._PAUSE_MAX_BUFFERED_SENTENCES

        handled = await h._buffer_sentence_if_paused("one too many")

        assert handled is True
        assert h._is_speaking is False, "overflow must run the full cancel path"
        assert h._llm_cancel_event.is_set()
        assert h._output_paused is False
        h.metrics.record_turn_interrupted.assert_called_once()


# ---------------------------------------------------------------------------
# RESUME — timeout with no confirming transcript re-sends the unheard tail
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestResumeAfterTimeout:
    async def test_resume_resends_unheard_tail_from_retained_audio(self) -> None:
        h = _make_handler(confirm_s=0.03)
        _seed_partially_heard_turn(h)

        await h._start_pause_resume()
        assert h._output_paused is True

        await _wait_until(lambda: not h._output_paused)

        assert h._output_paused is False
        assert h._is_speaking is True, "resume keeps the floor"
        # Resume offset was sentence index 1 → "Frase due." (replayed from
        # its start: sentence granularity) and "Frase tre." are re-sent.
        sent = [c.args[0] for c in h.audio_sender.send_audio.await_args_list]
        assert sent == [b"\x02" * 320, b"\x03" * 320, b"\x04" * 320]
        # The replayed sentences were re-stamped on the (frozen-then-
        # resumed) timeline; "Frase uno." kept its original stamp.
        assert [s[0] for s in h._turn_spoken_segments] == [
            "Frase uno.",
            "Frase due.",
            "Frase tre.",
        ]
        assert h._turn_spoken_segments[1][1] == pytest.approx(2.5, abs=0.1)
        # The re-sent tail advanced the playback cursor again.
        assert h._turn_playback_total_s > 2.5
        assert h._playback_buffered_until > 0.0

    async def test_resume_records_false_interruption_not_interruption(self) -> None:
        h = _make_handler(confirm_s=0.03)
        _seed_partially_heard_turn(h)

        await h._start_pause_resume()
        await _wait_until(lambda: not h._output_paused)

        # Backchannel path: overlap closed with was_interruption=False and
        # the interruption-side records never fired.
        h.metrics.record_overlap_end.assert_called_once()
        called = h.metrics.record_overlap_end.call_args
        if called.kwargs:
            assert called.kwargs.get("was_interruption") is False
        else:
            assert called.args[0] is False
        h.metrics.record_bargein_detected.assert_not_called()
        h.metrics.record_turn_interrupted.assert_not_called()
        h.metrics.record_tts_stopped.assert_not_called()
        h.metrics.anchor_user_speech_start.assert_called()
        assert h._barge_in_pending_since is None
        assert h._barge_in_pending_task is None

    async def test_resume_with_nothing_unheard_just_unpauses(self) -> None:
        """All stamped sentences fully played at pause time → nothing to
        re-send; the resume only releases the gate."""
        h = _make_handler(confirm_s=0.03)
        h._turn_spoken_segments = [("Frase uno.", 0.0)]
        h._turn_playback_total_s = 2.0
        h._playback_buffered_until = 0.0  # fully drained
        h._turn_sentence_audio = [
            {"text": "Frase uno.", "chunks": [b"\x01" * 320], "sent": 1}
        ]
        h._pause_retained_bytes = 320

        await h._start_pause_resume()
        assert h._pause_resume_index == 1
        await _wait_until(lambda: not h._output_paused)

        h.audio_sender.send_audio.assert_not_awaited()
        assert [s[0] for s in h._turn_spoken_segments] == ["Frase uno."]

    async def test_resume_rearms_grace_for_post_complete_turn(self) -> None:
        """A pause during the post-complete carrier backlog (dispatch done)
        must re-arm the #164 grace machinery for the re-sent tail."""
        h = _make_handler(confirm_s=0.03)
        _seed_partially_heard_turn(h)
        h._dispatch_task = None  # post-complete: no turn in flight

        await h._start_pause_resume()
        await _wait_until(lambda: not h._output_paused)
        try:
            assert h._grace_task is not None, (
                "resume must reschedule _end_speaking_with_grace for the backlog"
            )
            assert h._is_speaking is True
        finally:
            h._clear_grace_task()


# ---------------------------------------------------------------------------
# KILL — a committed final transcript within the window runs the cancel path
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestKillOnConfirmingTranscript:
    async def test_final_transcript_kills_paused_turn(self) -> None:
        h = _make_handler(confirm_s=10)
        _seed_partially_heard_turn(h)
        await h._start_pause_resume()

        await h._handle_barge_in(
            Transcript(text="please stop talking now", is_final=True, speech_final=True)
        )

        assert h._is_speaking is False
        assert h._llm_cancel_event.is_set()
        assert h._output_paused is False
        # Paused buffers discarded — no replay may survive the kill.
        assert h._turn_sentence_audio == []
        assert h._paused_sentences == []
        assert h._barge_in_pending_since is None
        assert h._barge_in_pending_task is None
        # Interruption metrics: overlap window anchored at PAUSE time was
        # preserved (started once) and closed as a real interruption.
        h.metrics.record_overlap_start.assert_called_once()
        h.metrics.record_overlap_end.assert_called_once()
        called = h.metrics.record_overlap_end.call_args
        if called.kwargs:
            assert called.kwargs.get("was_interruption") is True
        else:
            assert called.args[0] is True
        h.metrics.record_bargein_detected.assert_called_once()
        h.metrics.record_turn_interrupted.assert_called_once()

    async def test_kill_truncates_post_complete_history_to_frozen_heard_prefix(
        self,
    ) -> None:
        """Post-complete pause→kill: the cursor was frozen at pause time, so
        the history rewrite must still see the heard prefix (not skip on
        'no backlog')."""
        h = _make_handler(confirm_s=10)
        _seed_partially_heard_turn(h)
        full = "Frase uno. Frase due. Frase tre."
        h.conversation_history.append(
            {"role": "assistant", "text": full, "timestamp": time.time()}
        )
        h.transcript_entries.append({"role": "assistant", "text": full})

        await h._start_pause_resume()
        await h._handle_barge_in(
            Transcript(text="aspetta un attimo", is_final=True, speech_final=True)
        )

        expected = "Frase uno. Frase due. [interrupted by caller]"
        assert h.conversation_history[-1]["text"] == expected
        assert h.transcript_entries[-1]["text"] == expected

    async def test_interim_transcript_does_not_kill_while_paused(self) -> None:
        h = _make_handler(confirm_s=10)
        _seed_partially_heard_turn(h)
        await h._start_pause_resume()

        await h._handle_barge_in(
            Transcript(text="please stop", is_final=False, speech_final=False)
        )

        assert h._is_speaking is True
        assert h._output_paused is True
        assert not h._llm_cancel_event.is_set()
        h._clear_pending_barge_in()

    async def test_hallucination_final_does_not_kill_while_paused(self) -> None:
        h = _make_handler(confirm_s=10)
        _seed_partially_heard_turn(h)
        await h._start_pause_resume()

        # "Thanks for watching!" is in _STT_HALLUCINATIONS (Whisper caption
        # bias) — a paused turn must not be killed by it.
        await h._handle_barge_in(
            Transcript(text="Thanks for watching!", is_final=True, speech_final=True)
        )

        assert h._is_speaking is True
        assert h._output_paused is True
        h._clear_pending_barge_in()

    async def test_duplicate_of_last_commit_does_not_kill_while_paused(self) -> None:
        h = _make_handler(confirm_s=10)
        _seed_partially_heard_turn(h)
        h._last_commit_text = "che ore sono"
        h._last_commit_at = time.time() - 0.3
        await h._start_pause_resume()

        await h._handle_barge_in(
            Transcript(text="che ore sono", is_final=True, speech_final=True)
        )

        assert h._is_speaking is True
        assert h._output_paused is True
        h._clear_pending_barge_in()

    async def test_legacy_cancel_mode_is_untouched_by_final_gate(self) -> None:
        """In cancel mode (never paused) interim transcripts still cancel
        immediately — byte-identical legacy contract."""
        h = _make_handler(mode="cancel")

        await h._handle_barge_in(
            Transcript(text="okay wait", is_final=False, speech_final=False)
        )

        assert h._is_speaking is False
        assert h._llm_cancel_event.is_set()


# ---------------------------------------------------------------------------
# Buffer overflow — degrade rather than grow without bound
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRetainedAudioOverflow:
    async def test_overflow_while_paused_degrades_to_full_cancel(self) -> None:
        h = _make_handler(confirm_s=10)
        await h._start_pause_resume()
        entry = h._begin_retained_sentence("Frase uno.")
        assert entry is not None
        h._pause_retained_bytes = h._pause_retained_cap_bytes()  # at the cap

        retained = await h._retain_pause_chunk(entry, b"\x00" * 640)

        assert retained is False
        assert h._is_speaking is False, "paused overflow must kill the turn"
        assert h._llm_cancel_event.is_set()
        assert h._output_paused is False
        h.metrics.record_turn_interrupted.assert_called_once()

    async def test_overflow_while_speaking_releases_retention_for_turn(self) -> None:
        h = _make_handler(confirm_s=10)
        entry = h._begin_retained_sentence("Frase uno.")
        assert entry is not None
        h._pause_retained_bytes = h._pause_retained_cap_bytes()

        retained = await h._retain_pause_chunk(entry, b"\x00" * 640)

        assert retained is False
        assert h._is_speaking is True, "an un-paused overflow must NOT cancel"
        assert h._pause_resume_overflowed is True
        assert entry["chunks"] == []
        assert h._pause_retained_bytes == 0
        # With retention released, speech_start falls back to legacy cancel.
        assert h._should_pause_for_barge_in() is False

    async def test_cap_scales_with_wire_format(self) -> None:
        h = _make_handler()
        assert h._pause_retained_cap_bytes() == int(15.0 * 32_000)
        h.audio_sender._input_is_mulaw_8k = True
        assert h._pause_retained_cap_bytes() == int(15.0 * 8_000)


# ---------------------------------------------------------------------------
# Streaming-loop integration — pause mid-stream, then resume or kill
# ---------------------------------------------------------------------------


def _make_streaming_handler(monkeypatch, confirm_s: float) -> PipelineStreamHandler:
    """Real handler (full __init__) so _process_streaming_response runs the
    genuine loop; only the paid edges (TTS / carrier) are stubbed."""
    from collections import deque

    from tests.conftest import make_agent

    monkeypatch.setenv("PATTER_TTS_TAIL_GRACE_MS", "0")
    sender = AsyncMock()
    sender._input_is_mulaw_8k = False
    sender.reset_pcm_carry = MagicMock()
    handler = PipelineStreamHandler(
        agent=make_agent(
            barge_in_mode="pause_resume",
            barge_in_confirm_ms=int(confirm_s * 1000),
        ),
        audio_sender=sender,
        call_id="call-pause-resume",
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

    class _StubTTS:
        output_format = "pcm_16000"

        async def synthesize(self, _text: str):
            yield b"\x00" * 3200  # 100 ms PCM16 @ 16 kHz

    handler._tts = _StubTTS()
    return handler


@pytest.mark.unit
class TestStreamingLoopPauseIntegration:
    async def test_pause_mid_stream_then_resume_speaks_buffered_sentences(
        self, monkeypatch
    ) -> None:
        handler = _make_streaming_handler(monkeypatch, confirm_s=0.05)
        sender = handler.audio_sender

        async def _gen():
            yield "Frase uno. "
            # Cough: VAD speech_start lands after sentence one went out.
            await handler._start_pause_resume()
            yield "Frase due. "  # buffered as text while paused
            yield "Frase tre."

        text = await handler._process_streaming_response(_gen(), "call-pause-resume")

        assert handler._last_response_interrupted is False
        assert text == "Frase uno. Frase due. Frase tre."
        # Sentence one was sent live; the resume replayed it (it had not
        # finished playing when the pause cleared the carrier) and then the
        # buffered sentences flowed through the normal synth path.
        assert sender.send_audio.await_count >= 3
        assert handler._output_paused is False
        assert [s[0] for s in handler._turn_spoken_segments] == [
            "Frase uno. ".strip() and "Frase uno.",
            "Frase due.",
            "Frase tre.",
        ]

    async def test_pause_mid_stream_then_kill_marks_interrupted(
        self, monkeypatch
    ) -> None:
        handler = _make_streaming_handler(monkeypatch, confirm_s=10.0)

        async def _gen():
            yield "Frase uno. "
            await handler._start_pause_resume()
            yield "Frase due. "
            # Confirming final transcript arrives while paused. Backdate the
            # first-audio stamp so the 500 ms anti-flicker gate (which only
            # exists to reject same-instant phantoms) is open, as it would
            # be on a real call by transcript time.
            handler._first_audio_sent_at = time.time() - 1.0
            await handler._handle_barge_in(
                Transcript(
                    text="no aspetta fermati", is_final=True, speech_final=True
                )
            )
            yield "Frase tre."

        text = await handler._process_streaming_response(_gen(), "call-pause-resume")

        assert handler._last_response_interrupted is True
        assert "[interrupted by caller]" in text
        assert handler._is_speaking is False
        assert handler._output_paused is False
        assert handler._turn_sentence_audio == []
        assert handler._paused_sentences == []

    async def test_stream_ends_while_paused_waits_for_decision_then_resumes(
        self, monkeypatch
    ) -> None:
        """The turn must not end while the pause decision is outstanding —
        the flush-phase wait holds it until the resume timer fires, then the
        buffered sentence is spoken."""
        handler = _make_streaming_handler(monkeypatch, confirm_s=0.05)
        sender = handler.audio_sender

        async def _gen():
            yield "Frase uno. "
            await handler._start_pause_resume()
            yield "Frase due."
            # Stream ends here, still paused.

        text = await handler._process_streaming_response(_gen(), "call-pause-resume")

        assert handler._last_response_interrupted is False
        assert text == "Frase uno. Frase due."
        assert handler._output_paused is False
        spoken = [s[0] for s in handler._turn_spoken_segments]
        assert spoken == ["Frase uno.", "Frase due."]
        assert sender.send_audio.await_count >= 2


# ---------------------------------------------------------------------------
# Teardown — cleanup mid-pause leaks nothing
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCleanupMidPause:
    async def test_cleanup_cancels_resume_timer_and_discards_buffers(self) -> None:
        h = _make_handler(confirm_s=10)
        _seed_partially_heard_turn(h)
        h._stt_task = None
        h._tts = None
        h._remote_handler = None
        h._resampler_8k_to_16k = None
        h._first_message_mark_counter = 0

        await h._start_pause_resume()
        task = h._barge_in_pending_task
        assert task is not None and not task.done()
        h.metrics.record_overlap_end.reset_mock()
        h.audio_sender.send_audio.reset_mock()

        await h.cleanup()
        await asyncio.sleep(0)

        assert task.cancelled() or task.done()
        assert h._output_paused is False
        assert h._turn_sentence_audio == []
        assert h._barge_in_pending_task is None
        # No stale resume fired after teardown: nothing re-sent, no
        # overlap_end on a finalised metrics object.
        h.audio_sender.send_audio.assert_not_awaited()
        h.metrics.record_overlap_end.assert_not_called()
