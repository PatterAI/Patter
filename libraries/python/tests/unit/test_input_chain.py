"""Unit tests for the ``InputProcessingChain`` (pipeline-stages slice 1).

Covers the extracted inbound chain — decode (mulaw->PCM16) -> stateful
8k->16k resample -> AEC near-end -> ``agent.audio_filter`` -> VAD — plus the
handler-level wiring that finally makes ``Agent(audio_filter=...)`` a live
parameter:

  1. Stage ORDER: the filter runs AFTER AEC and BEFORE VAD (asserted via
     recording fakes), per the AudioFilter ABC docstring.
  2. Fail-open filter wrapper: a raising (or non-bytes-returning) filter
     degrades to passthrough with exactly ONE warning, and the frames keep
     flowing.
  3. Decode parity: mulaw and PCM inputs produce byte-identical output to
     the pre-extraction handler path (stateful resampler preserved across
     chunks; pure passthrough when nothing is configured).
  4. Handler regression: ``PipelineStreamHandler.on_audio_received`` now
     feeds the FILTERED bytes to STT — previously ``audio_filter`` was
     accepted, documented, and silently never invoked.
"""

from __future__ import annotations

import logging
from collections import deque
from unittest.mock import AsyncMock, MagicMock

import pytest

from getpatter.providers.base import VADEvent
from getpatter.services.input_chain import InputProcessingChain
from getpatter.stream_handler import PipelineStreamHandler
from getpatter.models import PipelineHooks

from tests.conftest import fake_mulaw_frame, fake_pcm_frame, make_agent


# ---------------------------------------------------------------------------
# Recording fakes
# ---------------------------------------------------------------------------


class _RecordingAec:
    """Fake AEC that tags frames and records call order."""

    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.seen: list[bytes] = []

    def process_near_end(self, pcm: bytes) -> bytes:
        self.order.append("aec")
        self.seen.append(pcm)
        return b"AEC:" + pcm


class _RecordingFilter:
    """Fake AudioFilter that tags frames and records call order + args."""

    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.seen: list[tuple[bytes, int]] = []

    async def process(self, pcm_chunk: bytes, sample_rate: int) -> bytes:
        self.order.append("filter")
        self.seen.append((pcm_chunk, sample_rate))
        return b"FLT:" + pcm_chunk

    async def close(self) -> None:  # pragma: no cover - interface completeness
        pass


class _RecordingVad:
    """Fake VAD that records the frames it is fed and emits scripted events."""

    def __init__(self, order: list[str], events: list[VADEvent | None] | None = None) -> None:
        self.order = order
        self.seen: list[tuple[bytes, int]] = []
        self.events = list(events or [])

    async def process_frame(self, pcm_chunk: bytes, sample_rate: int) -> VADEvent | None:
        self.order.append("vad")
        self.seen.append((pcm_chunk, sample_rate))
        return self.events.pop(0) if self.events else None

    async def close(self) -> None:  # pragma: no cover - interface completeness
        pass


class _RaisingFilter:
    """Fake AudioFilter whose ``process`` always raises (e.g. a frame-size
    contract violation in a native SDK)."""

    def __init__(self) -> None:
        self.calls = 0

    async def process(self, pcm_chunk: bytes, sample_rate: int) -> bytes:
        self.calls += 1
        raise ValueError("frame size mismatch: expected 160 samples, got 320")

    async def close(self) -> None:  # pragma: no cover - interface completeness
        pass


def _make_chain(
    *,
    input_is_mulaw_8k: bool = False,
    aec=None,
    audio_filter=None,
    vad=None,
) -> InputProcessingChain:
    return InputProcessingChain(
        input_is_mulaw_8k=input_is_mulaw_8k,
        get_aec=lambda: aec,
        get_audio_filter=lambda: audio_filter,
        get_vad=lambda: vad,
    )


# ---------------------------------------------------------------------------
# Stage order — AEC -> audio_filter -> VAD
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestStageOrder:
    async def test_filter_runs_after_aec_and_before_vad(self) -> None:
        order: list[str] = []
        aec = _RecordingAec(order)
        flt = _RecordingFilter(order)
        vad = _RecordingVad(order)
        chain = _make_chain(aec=aec, audio_filter=flt, vad=vad)
        frame = fake_pcm_frame(duration_ms=20)

        result = await chain.process(frame)

        assert order == ["aec", "filter", "vad"]
        # The filter saw the AEC output (not the raw frame) ...
        assert flt.seen == [(b"AEC:" + frame, 16000)]
        # ... and the VAD saw the FILTERED audio (noise suppression benefits
        # barge-in detection).
        assert vad.seen == [(b"FLT:AEC:" + frame, 16000)]
        # The frame handed downstream (self-hearing gate / STT) is the
        # filtered one.
        assert result.pcm == b"FLT:AEC:" + frame

    async def test_filter_applies_without_aec_or_vad(self) -> None:
        order: list[str] = []
        flt = _RecordingFilter(order)
        chain = _make_chain(audio_filter=flt)
        frame = fake_pcm_frame(duration_ms=20)

        result = await chain.process(frame)

        assert order == ["filter"]
        assert result.pcm == b"FLT:" + frame
        assert result.vad_event is None
        assert result.vad_configured is False

    async def test_filter_receives_pipeline_sample_rate(self) -> None:
        order: list[str] = []
        flt = _RecordingFilter(order)
        chain = _make_chain(audio_filter=flt)

        await chain.process(fake_pcm_frame(duration_ms=20))

        assert flt.seen[0][1] == 16000


# ---------------------------------------------------------------------------
# Fail-open filter wrapper — passthrough + warn-once
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestFilterFailOpen:
    async def test_raising_filter_passes_audio_through(self, caplog) -> None:
        flt = _RaisingFilter()
        chain = _make_chain(audio_filter=flt)
        frame = fake_pcm_frame(duration_ms=20)

        with caplog.at_level(logging.DEBUG, logger="getpatter"):
            result = await chain.process(frame)

        assert result.pcm == frame  # pre-filter audio, untouched
        assert flt.calls == 1

    async def test_filter_failure_warns_exactly_once(self, caplog) -> None:
        flt = _RaisingFilter()
        chain = _make_chain(audio_filter=flt)
        frame = fake_pcm_frame(duration_ms=20)

        with caplog.at_level(logging.DEBUG, logger="getpatter"):
            for _ in range(5):
                result = await chain.process(frame)
                assert result.pcm == frame

        warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "audio_filter" in r.message
        ]
        assert len(warnings) == 1, (
            f"expected exactly one WARN-once record, got "
            f"{[r.message for r in warnings]}"
        )
        # The filter keeps being attempted (transient failures may recover).
        assert flt.calls == 5
        # Subsequent failures are demoted to DEBUG.
        debugs = [
            r
            for r in caplog.records
            if r.levelno == logging.DEBUG and "audio_filter" in r.message
        ]
        assert len(debugs) == 4

    async def test_non_bytes_return_treated_as_failure(self, caplog) -> None:
        class _BadFilter:
            async def process(self, pcm_chunk: bytes, sample_rate: int):
                return None  # contract violation

            async def close(self) -> None:  # pragma: no cover
                pass

        chain = _make_chain(audio_filter=_BadFilter())
        frame = fake_pcm_frame(duration_ms=20)

        with caplog.at_level(logging.DEBUG, logger="getpatter"):
            result = await chain.process(frame)

        assert result.pcm == frame
        assert any(
            r.levelno == logging.WARNING and "audio_filter" in r.message
            for r in caplog.records
        )

    async def test_filter_failure_does_not_break_vad(self) -> None:
        order: list[str] = []
        vad = _RecordingVad(order, events=[VADEvent(type="speech_start")])
        chain = _make_chain(audio_filter=_RaisingFilter(), vad=vad)
        frame = fake_pcm_frame(duration_ms=20)

        result = await chain.process(frame)

        # VAD saw the unfiltered (passthrough) frame and still produced its event.
        assert vad.seen == [(frame, 16000)]
        assert result.vad_event is not None
        assert result.vad_event.type == "speech_start"
        assert result.vad_configured is True


# ---------------------------------------------------------------------------
# Decode parity — mulaw and PCM inputs
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestDecodeParity:
    async def test_pcm_input_is_pure_passthrough(self) -> None:
        chain = _make_chain(input_is_mulaw_8k=False)
        frame = fake_pcm_frame(duration_ms=20)

        result = await chain.process(frame)

        assert result.pcm == frame
        assert result.vad_event is None
        assert result.vad_configured is False

    async def test_mulaw_input_matches_stateful_reference_across_chunks(self) -> None:
        """Byte-identical to the pre-extraction handler path: per-call
        StatefulResampler state must carry across chunk boundaries."""
        from getpatter.audio.transcoding import (
            create_resampler_8k_to_16k,
            mulaw_to_pcm16,
        )

        chain = _make_chain(input_is_mulaw_8k=True)
        # Two distinct non-silence chunks so resampler state matters.
        chunk_a = bytes(range(160))  # 20 ms of mulaw @ 8 kHz
        chunk_b = bytes(reversed(range(160)))

        out_a = (await chain.process(chunk_a)).pcm
        out_b = (await chain.process(chunk_b)).pcm

        reference = create_resampler_8k_to_16k()
        ref_a = reference.process(mulaw_to_pcm16(chunk_a))
        ref_b = reference.process(mulaw_to_pcm16(chunk_b))

        assert out_a == ref_a
        assert out_b == ref_b

    async def test_mulaw_input_feeds_decoded_pcm_to_filter(self) -> None:
        order: list[str] = []
        flt = _RecordingFilter(order)
        chain = _make_chain(input_is_mulaw_8k=True, audio_filter=flt)
        mulaw = fake_mulaw_frame(duration_ms=20)

        result = await chain.process(mulaw)

        seen_pcm, seen_rate = flt.seen[0]
        assert seen_rate == 16000
        # The filter received decoded/upsampled PCM16 @ 16 kHz, not mulaw.
        assert seen_pcm != mulaw
        assert result.pcm == b"FLT:" + seen_pcm

    async def test_flush_resets_resampler_state(self) -> None:
        chain = _make_chain(input_is_mulaw_8k=True)
        first = (await chain.process(fake_mulaw_frame(duration_ms=20))).pcm
        chain.flush()
        # After flush the next chunk starts from a cold resampler — identical
        # output to the very first chunk.
        again = (await chain.process(fake_mulaw_frame(duration_ms=20))).pcm
        assert again == first


# ---------------------------------------------------------------------------
# VAD plumbing
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestVadPlumbing:
    async def test_vad_event_is_returned(self) -> None:
        order: list[str] = []
        vad = _RecordingVad(
            order, events=[VADEvent(type="speech_start"), None]
        )
        chain = _make_chain(vad=vad)

        first = await chain.process(fake_pcm_frame(duration_ms=20))
        second = await chain.process(fake_pcm_frame(duration_ms=20))

        assert first.vad_event is not None
        assert first.vad_event.type == "speech_start"
        assert first.vad_configured is True
        assert second.vad_event is None
        assert second.vad_configured is True

    async def test_vad_exception_is_swallowed_per_frame(self) -> None:
        class _BrokenVad:
            async def process_frame(self, pcm_chunk: bytes, sample_rate: int):
                raise RuntimeError("onnx blew up")

            async def close(self) -> None:  # pragma: no cover
                pass

        chain = _make_chain(vad=_BrokenVad())
        frame = fake_pcm_frame(duration_ms=20)

        result = await chain.process(frame)

        assert result.pcm == frame
        assert result.vad_event is None
        assert result.vad_configured is True  # gate semantics unchanged

    async def test_late_bound_getters_see_post_construction_providers(self) -> None:
        """``start()`` / tests install AEC + VAD AFTER the chain exists —
        the getters must observe them (no construction-time capture)."""
        holder: dict[str, object] = {"aec": None, "vad": None}
        chain = InputProcessingChain(
            input_is_mulaw_8k=False,
            get_aec=lambda: holder["aec"],
            get_audio_filter=lambda: None,
            get_vad=lambda: holder["vad"],
        )
        frame = fake_pcm_frame(duration_ms=20)
        before = await chain.process(frame)
        assert before.pcm == frame and before.vad_configured is False

        order: list[str] = []
        holder["aec"] = _RecordingAec(order)
        holder["vad"] = _RecordingVad(order)
        after = await chain.process(frame)

        assert order == ["aec", "vad"]
        assert after.pcm == b"AEC:" + frame
        assert after.vad_configured is True


# ---------------------------------------------------------------------------
# Handler wiring — Agent(audio_filter=...) finally transforms the STT bytes
# ---------------------------------------------------------------------------


def _make_handler(agent, *, input_is_mulaw_8k: bool = False) -> PipelineStreamHandler:
    handler = PipelineStreamHandler(
        agent=agent,
        audio_sender=AsyncMock(),
        call_id="call-filter",
        caller="+15551110000",
        callee="+15552220000",
        resolved_prompt="p",
        metrics=None,
        for_twilio=input_is_mulaw_8k,
        input_is_mulaw_8k=input_is_mulaw_8k,
        conversation_history=deque(maxlen=10),
        transcript_entries=deque(maxlen=10),
    )
    handler._stt = MagicMock()
    handler._stt.send_audio = AsyncMock()
    return handler


@pytest.mark.unit
@pytest.mark.asyncio
class TestHandlerAudioFilterWiring:
    """Regression for the dead parameter: ``agent.audio_filter`` was accepted
    by the public API and documented as "integrated before VAD and STT" but
    never invoked by the pipeline."""

    async def test_audio_filter_transforms_bytes_reaching_stt(self) -> None:
        order: list[str] = []
        flt = _RecordingFilter(order)
        handler = _make_handler(make_agent(audio_filter=flt))
        frame = fake_pcm_frame(duration_ms=20)

        await handler.on_audio_received(frame)

        # The filter ran on the inbound frame ...
        assert flt.seen == [(frame, 16000)]
        # ... and STT received the TRANSFORMED bytes, not the raw frame.
        handler._stt.send_audio.assert_awaited_once_with(b"FLT:" + frame)

    async def test_audio_filter_applies_on_mulaw_input(self) -> None:
        order: list[str] = []
        flt = _RecordingFilter(order)
        handler = _make_handler(
            make_agent(audio_filter=flt), input_is_mulaw_8k=True
        )

        await handler.on_audio_received(fake_mulaw_frame(duration_ms=20))

        # Filter saw decoded PCM16 @ 16 kHz; STT got the filtered version.
        decoded_pcm = flt.seen[0][0]
        handler._stt.send_audio.assert_awaited_once_with(b"FLT:" + decoded_pcm)

    async def test_failing_filter_falls_back_to_unfiltered_audio(
        self, caplog
    ) -> None:
        handler = _make_handler(make_agent(audio_filter=_RaisingFilter()))
        frame = fake_pcm_frame(duration_ms=20)

        with caplog.at_level(logging.DEBUG, logger="getpatter"):
            await handler.on_audio_received(frame)
            await handler.on_audio_received(frame)

        # Both frames reached STT unfiltered — the call stays alive.
        assert handler._stt.send_audio.await_count == 2
        for await_call in handler._stt.send_audio.await_args_list:
            assert await_call.args == (frame,)
        warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "audio_filter" in r.message
        ]
        assert len(warnings) == 1

    async def test_no_filter_keeps_bytes_identical(self) -> None:
        """Byte-identical regression guard: with no AEC/filter/VAD the frame
        must reach STT untouched (same as before the chain extraction)."""
        handler = _make_handler(make_agent())
        frame = fake_pcm_frame(duration_ms=20)

        await handler.on_audio_received(frame)

        handler._stt.send_audio.assert_awaited_once_with(frame)

    async def test_before_send_to_stt_hook_sees_filtered_audio(self) -> None:
        """The pre-STT hook contract ("receives the PCM chunk before it
        reaches STT") now naturally includes the filter's output."""
        seen: list[bytes] = []

        def _hook(audio: bytes, ctx) -> bytes:
            seen.append(audio)
            return audio

        order: list[str] = []
        flt = _RecordingFilter(order)
        handler = _make_handler(
            make_agent(
                audio_filter=flt,
                hooks=PipelineHooks(before_send_to_stt=_hook),
            )
        )
        frame = fake_pcm_frame(duration_ms=20)

        await handler.on_audio_received(frame)

        assert seen == [b"FLT:" + frame]
        handler._stt.send_audio.assert_awaited_once_with(b"FLT:" + frame)

    async def test_cleanup_flushes_input_chain(self) -> None:
        handler = _make_handler(make_agent(), input_is_mulaw_8k=True)
        handler._tts = None
        handler._remote_handler = None
        await handler.on_audio_received(fake_mulaw_frame(duration_ms=20))
        assert handler._input_chain is not None

        # STT stub must tolerate close() during cleanup.
        handler._stt.close = AsyncMock()
        await handler.cleanup()

        assert handler._input_chain is None
