"""Pipeline-handler integration tests for semantic turn detection.

Exercises ``PipelineStreamHandler``'s opt-in ``agent.turn_detector`` wiring
with a fake detector (scripted probabilities) and a scripted VAD — no ONNX,
no network:

  1. Default path (no detector): VAD ``speech_end`` finalizes STT
     immediately — byte-identical to previous releases.
  2. Detector ≥ threshold on speech_end: finalize fires once and the EOU
     trigger is stamped ``semantic_turn_detector``.
  3. Detector < threshold: finalize is HELD; after each subsequent
     ~200 ms silence window the detector is re-polled, and a flip to
     ≥ threshold finalizes with the semantic trigger.
  4. ``speech_start`` during a hold cancels it (the user resumed) without
     ever finalizing.
  5. The hard cap ``max_semantic_hold_ms`` force-finalizes (frame-driven
     and wall-clock backstop variants) with the ``vad_silence`` trigger so
     a turn can never hang.
  6. A raising detector fails OPEN (immediate finalize, ``vad_silence``).
  7. The rolling audio window stays bounded at 8 s of PCM16-16k.
  8. ``_dispatch_turn`` fires ``on_user_speech_eos`` with the stamped
     trigger and resets it.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from unittest.mock import AsyncMock, MagicMock

import pytest

from getpatter._speech_events import EouTrigger, SpeechEvents
from getpatter.providers.base import TurnDetectorProvider, VADEvent, VADProvider
from getpatter.stream_handler import PipelineStreamHandler

from tests.conftest import fake_pcm_frame, make_agent

# STT adapter / telephony sender — the external boundaries — are mocked.
pytestmark = pytest.mark.mocked


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeTurnDetector(TurnDetectorProvider):
    """Scripted end-of-turn probabilities; records every prediction window."""

    def __init__(self, probs, threshold: float = 0.5, raise_on_call: bool = False):
        self._probs = list(probs)
        self._threshold = threshold
        self._raise = raise_on_call
        self.windows: list[bytes] = []
        self.attempts: int = 0  # counted even when predict raises

    @property
    def threshold(self) -> float:
        return self._threshold

    async def predict(self, pcm16_16k_window: bytes) -> float:
        self.attempts += 1
        if self._raise:
            raise RuntimeError("model exploded")
        self.windows.append(pcm16_16k_window)
        if len(self._probs) > 1:
            return self._probs.pop(0)
        return self._probs[0]

    async def close(self) -> None:
        return None


class ScriptedVAD(VADProvider):
    """Returns one scripted event (or None) per ``process_frame`` call."""

    def __init__(self, events):
        self._events = list(events)

    async def process_frame(self, pcm_chunk: bytes, sample_rate: int):
        if self._events:
            return self._events.pop(0)
        return None

    async def close(self) -> None:
        return None


def _speech_end() -> VADEvent:
    return VADEvent(type="speech_end", confidence=0.1, duration_ms=400.0)


def _speech_start() -> VADEvent:
    return VADEvent(type="speech_start", confidence=0.9, duration_ms=250.0)


def _make_handler(
    *,
    vad_events=(),
    turn_detector=None,
    max_semantic_hold_ms: int = 1200,
) -> PipelineStreamHandler:
    handler = PipelineStreamHandler(
        agent=make_agent(
            vad=ScriptedVAD(vad_events),
            turn_detector=turn_detector,
            max_semantic_hold_ms=max_semantic_hold_ms,
        ),
        audio_sender=AsyncMock(),
        call_id="call-semantic",
        caller="+15551110000",
        callee="+15552220000",
        resolved_prompt="p",
        metrics=None,
        for_twilio=False,
        input_is_mulaw_8k=False,
        conversation_history=deque(maxlen=10),
        transcript_entries=deque(maxlen=10),
    )
    handler._stt = MagicMock()
    handler._stt.send_audio = AsyncMock()
    handler._stt.finalize = MagicMock(return_value=None)
    return handler


FRAME = fake_pcm_frame(duration_ms=20)  # 640 bytes of PCM16 @ 16 kHz


# ---------------------------------------------------------------------------
# 1. Default path — no detector configured
# ---------------------------------------------------------------------------


async def test_speech_end_without_detector_finalizes_immediately() -> None:
    handler = _make_handler(vad_events=[_speech_end()])
    await handler.on_audio_received(FRAME)
    handler._stt.finalize.assert_called_once()
    assert handler._last_eou_trigger == "vad_silence"
    assert handler._semantic_hold_active is False


async def test_no_detector_keeps_rolling_buffer_empty() -> None:
    handler = _make_handler(vad_events=[None, None])
    await handler.on_audio_received(FRAME)
    await handler.on_audio_received(FRAME)
    assert handler._semantic_audio_ring_bytes == 0


# ---------------------------------------------------------------------------
# 2. Semantic approve on the speech_end edge
# ---------------------------------------------------------------------------


async def test_high_probability_finalizes_with_semantic_trigger() -> None:
    detector = FakeTurnDetector(probs=[0.9])
    handler = _make_handler(vad_events=[None, _speech_end()], turn_detector=detector)

    await handler.on_audio_received(FRAME)  # buffered, no VAD event
    handler._stt.finalize.assert_not_called()
    await handler.on_audio_received(FRAME)  # speech_end → predict → finalize

    handler._stt.finalize.assert_called_once()
    assert handler._last_eou_trigger == "semantic_turn_detector"
    assert handler._semantic_hold_active is False
    # The detector saw the rolling window accumulated so far (2 frames).
    assert len(detector.windows) == 1
    assert detector.windows[0] == FRAME + FRAME


async def test_probability_at_threshold_counts_as_complete() -> None:
    detector = FakeTurnDetector(probs=[0.5], threshold=0.5)
    handler = _make_handler(vad_events=[_speech_end()], turn_detector=detector)
    await handler.on_audio_received(FRAME)
    handler._stt.finalize.assert_called_once()
    assert handler._last_eou_trigger == "semantic_turn_detector"


# ---------------------------------------------------------------------------
# 3. Hold + re-poll on subsequent silence windows
# ---------------------------------------------------------------------------


async def test_low_probability_holds_then_finalizes_on_repoll() -> None:
    detector = FakeTurnDetector(probs=[0.2, 0.9])
    events = [_speech_end()] + [None] * 30
    handler = _make_handler(vad_events=events, turn_detector=detector)

    await handler.on_audio_received(FRAME)  # speech_end → p=0.2 → hold
    handler._stt.finalize.assert_not_called()
    assert handler._semantic_hold_active is True

    # 9 more silent frames = 180 ms < 200 ms poll window → still holding.
    for _ in range(9):
        await handler.on_audio_received(FRAME)
    handler._stt.finalize.assert_not_called()
    assert len(detector.windows) == 1

    # 10th frame crosses the 200 ms silence window → re-poll → p=0.9.
    await handler.on_audio_received(FRAME)
    handler._stt.finalize.assert_called_once()
    assert len(detector.windows) == 2
    assert handler._last_eou_trigger == "semantic_turn_detector"
    assert handler._semantic_hold_active is False
    # The backstop task was cancelled with the hold.
    assert handler._semantic_hold_task is None


async def test_hold_repolls_keep_extending_below_threshold() -> None:
    detector = FakeTurnDetector(probs=[0.2, 0.3, 0.25])
    events = [_speech_end()] + [None] * 40
    handler = _make_handler(vad_events=events, turn_detector=detector)

    await handler.on_audio_received(FRAME)
    for _ in range(20):  # two full 200 ms windows → two re-polls
        await handler.on_audio_received(FRAME)

    handler._stt.finalize.assert_not_called()
    assert handler._semantic_hold_active is True
    assert len(detector.windows) == 3  # initial + 2 re-polls


# ---------------------------------------------------------------------------
# 4. speech_start cancels the hold (user resumed)
# ---------------------------------------------------------------------------


async def test_speech_start_during_hold_cancels_without_finalize() -> None:
    detector = FakeTurnDetector(probs=[0.1])
    handler = _make_handler(
        vad_events=[_speech_end(), _speech_start()], turn_detector=detector
    )

    await handler.on_audio_received(FRAME)  # hold begins
    assert handler._semantic_hold_active is True
    backstop = handler._semantic_hold_task

    await handler.on_audio_received(FRAME)  # user resumed speaking
    assert handler._semantic_hold_active is False
    handler._stt.finalize.assert_not_called()
    # Backstop task is cancelled so it can never fire later.
    if backstop is not None:
        await asyncio.sleep(0)
        assert backstop.cancelled() or backstop.done()


# ---------------------------------------------------------------------------
# 5. Hard cap — the turn can never hang
# ---------------------------------------------------------------------------


async def test_cap_finalizes_with_vad_silence_trigger_via_frames() -> None:
    detector = FakeTurnDetector(probs=[0.1])
    events = [_speech_end()] + [None] * 10
    handler = _make_handler(
        vad_events=events, turn_detector=detector, max_semantic_hold_ms=1
    )

    await handler.on_audio_received(FRAME)  # hold begins (cap = 1 ms)
    await asyncio.sleep(0.02)  # let the 1 ms deadline lapse
    await handler.on_audio_received(FRAME)  # frame path hits the cap

    handler._stt.finalize.assert_called_once()
    assert handler._last_eou_trigger == "vad_silence"
    assert handler._semantic_hold_active is False


async def test_cap_backstop_fires_even_without_further_frames() -> None:
    """If inbound audio stalls entirely mid-hold, the wall-clock backstop
    still finalizes at the cap — the turn cannot hang."""
    detector = FakeTurnDetector(probs=[0.1])
    handler = _make_handler(
        vad_events=[_speech_end()], turn_detector=detector, max_semantic_hold_ms=20
    )

    await handler.on_audio_received(FRAME)  # hold begins; no more frames
    handler._stt.finalize.assert_not_called()
    await asyncio.sleep(0.1)

    handler._stt.finalize.assert_called_once()
    assert handler._last_eou_trigger == "vad_silence"
    assert handler._semantic_hold_active is False


async def test_finalize_fires_exactly_once_when_cap_and_frames_race() -> None:
    detector = FakeTurnDetector(probs=[0.1])
    events = [_speech_end()] + [None] * 30
    handler = _make_handler(
        vad_events=events, turn_detector=detector, max_semantic_hold_ms=10
    )

    await handler.on_audio_received(FRAME)
    await asyncio.sleep(0.05)  # backstop fires
    for _ in range(15):  # frame path observes an already-resolved hold
        await handler.on_audio_received(FRAME)

    handler._stt.finalize.assert_called_once()


# ---------------------------------------------------------------------------
# 6. Detector failure fails OPEN
# ---------------------------------------------------------------------------


async def test_predict_exception_falls_back_to_immediate_finalize() -> None:
    detector = FakeTurnDetector(probs=[0.9], raise_on_call=True)
    handler = _make_handler(vad_events=[_speech_end()], turn_detector=detector)
    await handler.on_audio_received(FRAME)
    handler._stt.finalize.assert_called_once()
    assert handler._last_eou_trigger == "vad_silence"
    assert handler._semantic_hold_active is False


async def test_predict_failure_disables_detector_for_rest_of_call(caplog) -> None:
    """Fail ONCE: the first predict error logs a single clear warning and
    permanently rejoins the plain VAD-silence path — a broken runtime (e.g.
    onnxruntime missing/incompatible at call time) must not warn per turn
    nor be retried on every speech_end."""
    detector = FakeTurnDetector(probs=[0.9], raise_on_call=True)
    handler = _make_handler(
        vad_events=[_speech_end(), None, _speech_end()], turn_detector=detector
    )

    with caplog.at_level(logging.WARNING, logger="getpatter"):
        await handler.on_audio_received(FRAME)  # predict raises → warn + disable
        await handler.on_audio_received(FRAME)  # plain frame — no detector calls
        await handler.on_audio_received(FRAME)  # second speech_end → legacy path

    assert detector.attempts == 1  # never consulted again after the failure
    assert handler._semantic_detector_failed is True
    assert handler._stt.finalize.call_count == 2  # both turns still finalized
    assert handler._last_eou_trigger == "vad_silence"
    # Disabled ⇒ the rolling window stops accumulating too.
    assert handler._semantic_audio_ring_bytes == 0
    warnings = [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and "Semantic turn detector" in r.getMessage()
    ]
    assert len(warnings) == 1
    assert "disabling it for this call" in warnings[0].getMessage()


# ---------------------------------------------------------------------------
# 7. Rolling window stays bounded
# ---------------------------------------------------------------------------


async def test_rolling_buffer_bounded_to_8_seconds() -> None:
    detector = FakeTurnDetector(probs=[0.9])
    handler = _make_handler(vad_events=[None] * 500, turn_detector=detector)

    frame = fake_pcm_frame(duration_ms=20)
    for _ in range(500):  # 10 s of audio > the 8 s budget
        await handler.on_audio_received(frame)

    max_bytes = 16000 * 2 * 8
    assert handler._semantic_audio_ring_bytes <= max_bytes
    assert len(handler._semantic_window_bytes()) == handler._semantic_audio_ring_bytes
    # 8 s of 20 ms frames = 400 frames retained.
    assert handler._semantic_audio_ring_bytes == 400 * len(frame)


# ---------------------------------------------------------------------------
# 8. EOU trigger stamped on the committed turn (speech events)
# ---------------------------------------------------------------------------


async def test_dispatch_turn_fires_eos_with_semantic_trigger_and_resets() -> None:
    detector = FakeTurnDetector(probs=[0.9])
    handler = _make_handler(vad_events=[_speech_end()], turn_detector=detector)

    captured: list[dict] = []
    events = SpeechEvents()

    async def _on_eos(payload: dict) -> None:
        captured.append(payload)

    events.on_user_speech_eos = _on_eos
    handler.speech_events = events

    await handler.on_audio_received(FRAME)  # semantic finalize stamps trigger
    assert handler._last_eou_trigger == "semantic_turn_detector"

    # No on_message / llm_loop configured — the turn is discarded after the
    # EOS stamp, which is exactly the integration point under test.
    await handler._dispatch_turn("ciao, ho finito")

    assert len(captured) == 1
    assert captured[0]["trigger"] == "semantic_turn_detector"
    assert captured[0]["trigger"] == EouTrigger.SEMANTIC_TURN_DETECTOR
    assert captured[0]["transcript_so_far"] == "ciao, ho finito"
    # Consume-once semantics: the stamp resets for the next turn …
    assert handler._last_eou_trigger == "vad_silence"
    # … and the per-turn rolling window restarts.
    assert handler._semantic_audio_ring_bytes == 0


async def test_dispatch_turn_fires_eos_with_vad_silence_when_model_never_agreed() -> None:
    detector = FakeTurnDetector(probs=[0.1])
    handler = _make_handler(
        vad_events=[_speech_end()], turn_detector=detector, max_semantic_hold_ms=1
    )

    captured: list[dict] = []
    events = SpeechEvents()
    events.on_user_speech_eos = lambda payload: captured.append(payload)
    handler.speech_events = events

    await handler.on_audio_received(FRAME)  # hold begins
    await asyncio.sleep(0.05)  # backstop cap → vad_silence finalize
    await handler._dispatch_turn("pronto?")

    assert len(captured) == 1
    assert captured[0]["trigger"] == "vad_silence"


async def test_dispatch_turn_without_detector_fires_eos_with_vad_silence() -> None:
    """Default path: pipeline mode emits the committed-EOS speech event
    unconditionally (wired alongside the other speech edges); without a
    detector the trigger reflects the active VAD's silence endpointing —
    never the semantic trigger."""
    handler = _make_handler(vad_events=[_speech_end()])

    captured: list[dict] = []
    events = SpeechEvents()
    events.on_user_speech_eos = lambda payload: captured.append(payload)
    handler.speech_events = events

    await handler.on_audio_received(FRAME)
    await handler._dispatch_turn("ciao")

    assert len(captured) == 1
    assert captured[0]["trigger"] == "vad_silence"


async def test_committed_transcript_supersedes_active_hold() -> None:
    """STT endpointing on its own (e.g. Deepgram speech_final) during a hold
    cancels the hold — the dispatch path must not leave a live backstop."""
    detector = FakeTurnDetector(probs=[0.1])
    handler = _make_handler(
        vad_events=[_speech_end()], turn_detector=detector, max_semantic_hold_ms=60_000
    )

    await handler.on_audio_received(FRAME)  # hold begins (cap far away)
    assert handler._semantic_hold_active is True

    await handler._dispatch_turn("gia' finito")

    assert handler._semantic_hold_active is False
    assert handler._semantic_hold_task is None
    handler._stt.finalize.assert_not_called()


# ---------------------------------------------------------------------------
# Teardown safety
# ---------------------------------------------------------------------------


async def test_cleanup_cancels_active_hold_backstop() -> None:
    detector = FakeTurnDetector(probs=[0.1])
    handler = _make_handler(
        vad_events=[_speech_end()], turn_detector=detector, max_semantic_hold_ms=60_000
    )
    handler._stt.close = AsyncMock()

    await handler.on_audio_received(FRAME)
    assert handler._semantic_hold_active is True
    task = handler._semantic_hold_task

    await handler.cleanup()

    assert handler._semantic_hold_active is False
    if task is not None:
        await asyncio.sleep(0)
        assert task.cancelled() or task.done()
    handler._stt.finalize.assert_not_called()
