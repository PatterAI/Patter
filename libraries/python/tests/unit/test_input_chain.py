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

    def __init__(
        self, order: list[str], events: list[VADEvent | None] | None = None
    ) -> None:
        self.order = order
        self.seen: list[tuple[bytes, int]] = []
        self.events = list(events or [])

    async def process_frame(
        self, pcm_chunk: bytes, sample_rate: int
    ) -> VADEvent | None:
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
    input_sample_rate: int = 16000,
    high_pass_hz: int | None = None,
    agc=None,
) -> InputProcessingChain:
    return InputProcessingChain(
        input_is_mulaw_8k=input_is_mulaw_8k,
        get_aec=lambda: aec,
        get_audio_filter=lambda: audio_filter,
        get_vad=lambda: vad,
        input_sample_rate=input_sample_rate,
        high_pass_hz=high_pass_hz,
        agc=agc,
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
        vad = _RecordingVad(order, events=[VADEvent(type="speech_start"), None])
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
        handler = _make_handler(make_agent(audio_filter=flt), input_is_mulaw_8k=True)

        await handler.on_audio_received(fake_mulaw_frame(duration_ms=20))

        # Filter saw decoded PCM16 @ 16 kHz; STT got the filtered version.
        decoded_pcm = flt.seen[0][0]
        handler._stt.send_audio.assert_awaited_once_with(b"FLT:" + decoded_pcm)

    async def test_failing_filter_falls_back_to_unfiltered_audio(self, caplog) -> None:
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


# ---------------------------------------------------------------------------
# New APM stages — high-pass / DC-block, AGC, inbound sample-rate validation
# ---------------------------------------------------------------------------

import array as _array
import math as _math

from getpatter.models import AgcConfig


def _sine_frames(dbfs: float, freq_hz: float, frames: int, n: int = 320) -> list[bytes]:
    full = 32768.0
    amp = full * (10.0 ** (dbfs / 20.0)) * _math.sqrt(2.0)
    out = []
    idx = 0
    for _ in range(frames):
        buf = _array.array("h", bytes(n * 2))
        for i in range(n):
            buf[i] = int(round(amp * _math.sin(2.0 * _math.pi * freq_hz * idx / 16000)))
            idx += 1
        out.append(buf.tobytes())
    return out


def _rms(pcm: bytes) -> float:
    s = _array.array("h")
    s.frombytes(pcm)
    if not s:
        return 0.0
    return _math.sqrt(sum(float(x) * float(x) for x in s) / len(s))


class _PassRecordingFilter:
    """Identity AudioFilter (returns its input unchanged) that records order."""

    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.last: bytes = b""

    async def process(self, pcm_chunk: bytes, sample_rate: int) -> bytes:
        self.order.append("filter")
        self.last = pcm_chunk
        return pcm_chunk

    async def close(self) -> None:  # pragma: no cover
        pass


@pytest.mark.unit
@pytest.mark.asyncio
class TestHighPassStage:
    async def test_low_frequency_is_attenuated_in_chain(self) -> None:
        chain = _make_chain(high_pass_hz=100)  # PCM16 @ 16k passthrough path
        frames = _sine_frames(-12.0, 50.0, 30)  # 0.6 s of 50 Hz
        in_rms = _rms(b"".join(frames))
        parts = []
        for f in frames:
            parts.append((await chain.process(f)).pcm)
        out = b"".join(parts)
        # Skip the first 0.1 s settling transient.
        atten = 20.0 * _math.log10(_rms(out[1600 * 2 :]) / in_rms)
        assert atten < -10.0, f"expected >10 dB attenuation, got {atten:.1f} dB"

    async def test_speech_tone_passes_through(self) -> None:
        chain = _make_chain(high_pass_hz=100)
        frames = _sine_frames(-12.0, 440.0, 30)
        in_rms = _rms(b"".join(frames))
        parts = []
        for f in frames:
            parts.append((await chain.process(f)).pcm)
        out = b"".join(parts)
        atten = 20.0 * _math.log10(_rms(out[1600 * 2 :]) / in_rms)
        assert abs(atten) < 1.0, f"expected ~0 dB at 440 Hz, got {atten:.2f} dB"

    async def test_high_pass_is_length_preserving_on_mulaw_path(self) -> None:
        """HPF runs at 8 kHz BEFORE the resampler: framing is unchanged, so the
        chain output length matches the no-HPF mulaw path."""
        plain = _make_chain(input_is_mulaw_8k=True)
        hp = _make_chain(input_is_mulaw_8k=True, high_pass_hz=100)
        mulaw = fake_mulaw_frame(duration_ms=20)
        plain_out = (await plain.process(mulaw)).pcm
        hp_out = (await hp.process(mulaw)).pcm
        assert len(hp_out) == len(plain_out)

    async def test_invalid_cutoff_disables_stage_fail_open(self, caplog) -> None:
        # 9000 Hz > 8 kHz mulaw Nyquist → stage disabled, call still works.
        with caplog.at_level(logging.WARNING, logger="getpatter"):
            chain = _make_chain(input_is_mulaw_8k=True, high_pass_hz=9000)
        out = (await chain.process(fake_mulaw_frame(duration_ms=20))).pcm
        assert len(out) > 0  # audio still flows
        assert any("high_pass_hz" in r.message for r in caplog.records)


@pytest.mark.unit
@pytest.mark.asyncio
class TestAgcStage:
    async def test_quiet_input_is_boosted_toward_target(self) -> None:
        chain = _make_chain(agc=AgcConfig())  # target -18 dBFS
        frames = _sine_frames(-30.0, 220.0, 200)
        last = b""
        for f in frames:
            last = (await chain.process(f)).pcm
        out_dbfs = 20.0 * _math.log10(_rms(last) / 32768.0)
        assert abs(out_dbfs - (-18.0)) < 2.5, f"got {out_dbfs:.1f} dBFS"

    async def test_agc_runs_after_filter_and_before_vad(self) -> None:
        order: list[str] = []
        flt = _PassRecordingFilter(order)
        vad = _RecordingVad(order)
        chain = _make_chain(audio_filter=flt, vad=vad, agc=AgcConfig())
        frames = _sine_frames(-30.0, 220.0, 200)
        result = None
        for f in frames:
            result = await chain.process(f)
        # Recording stages fire filter-then-vad each frame.
        assert order[-2:] == ["filter", "vad"]
        # The VAD saw a LOUDER frame than the filter output → AGC sat between
        # noise suppression and VAD, and the returned frame is the AGC output.
        assert _rms(vad.seen[-1][0]) > _rms(flt.last) * 2.0
        assert result.pcm == vad.seen[-1][0]

    async def test_agc_off_by_default_keeps_bytes_identical(self) -> None:
        chain = _make_chain()  # no agc
        frame = _sine_frames(-30.0, 220.0, 1)[0]
        assert (await chain.process(frame)).pcm == frame


@pytest.mark.unit
@pytest.mark.asyncio
class TestInboundSampleRate:
    async def test_pcm_8k_input_is_resampled_to_16k(self) -> None:
        chain = _make_chain(input_is_mulaw_8k=False, input_sample_rate=8000)
        frame = fake_pcm_frame(duration_ms=20, sample_rate=8000)  # 160 samples
        out = (await chain.process(frame)).pcm
        # 8 kHz → 16 kHz roughly doubles the sample count.
        assert len(out) > len(frame) * 1.8

    async def test_pcm_16k_input_is_passthrough(self) -> None:
        chain = _make_chain(input_is_mulaw_8k=False, input_sample_rate=16000)
        frame = fake_pcm_frame(duration_ms=20, sample_rate=16000)
        assert (await chain.process(frame)).pcm == frame

    async def test_invalid_input_sample_rate_rejected(self) -> None:
        with pytest.raises(ValueError):
            _make_chain(input_sample_rate=0)
