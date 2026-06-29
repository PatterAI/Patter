"""Unit tests for the audio-format contract (``getpatter.audio.format``).

Parity with TypeScript ``tests/rate-aware-resampler.test.ts`` (format section).
Exercises the real resolution logic — no mocks.
"""

from __future__ import annotations

import pytest

from getpatter.audio.format import (
    AudioFormat,
    CARRIER_WIRE_FORMAT,
    LEGACY_PIPELINE_TTS_FORMAT,
    formats_match,
    parse_wire_format_string,
    resolve_tts_source_format,
)


@pytest.mark.unit
class TestCarrierWireFormat:
    def test_every_pstn_carrier_is_mulaw_8k(self) -> None:
        for carrier in ("twilio", "telnyx", "plivo"):
            assert CARRIER_WIRE_FORMAT[carrier] == AudioFormat("mulaw", 8000)


@pytest.mark.unit
class TestParseWireFormatString:
    def test_ulaw_and_mulaw_aliases(self) -> None:
        assert parse_wire_format_string("ulaw_8000") == AudioFormat("mulaw", 8000)
        assert parse_wire_format_string("mulaw_8000") == AudioFormat("mulaw", 8000)

    def test_pcm_rates(self) -> None:
        assert parse_wire_format_string("pcm_16000") == AudioFormat("pcm_s16le", 16000)
        assert parse_wire_format_string("pcm_22050") == AudioFormat("pcm_s16le", 22050)
        assert parse_wire_format_string("pcm16_8000") == AudioFormat("pcm_s16le", 8000)

    def test_unrecognised_returns_none(self) -> None:
        assert parse_wire_format_string("mp3_44100_128") is None


@pytest.mark.unit
class TestResolveTtsSourceFormat:
    def test_prefers_source_audio_format(self) -> None:
        class _Tts:
            def source_audio_format(self) -> AudioFormat:
                return AudioFormat("pcm_s16le", 24000)

        assert resolve_tts_source_format(_Tts()) == AudioFormat("pcm_s16le", 24000)

    def test_falls_back_to_output_format_string(self) -> None:
        class _Tts:
            output_format = "ulaw_8000"

        assert resolve_tts_source_format(_Tts()) == AudioFormat("mulaw", 8000)

    def test_falls_back_to_sample_rate_field(self) -> None:
        class _Tts:
            sample_rate = 8000

        assert resolve_tts_source_format(_Tts()) == AudioFormat("pcm_s16le", 8000)

    def test_unknown_adapter_is_legacy_16k(self) -> None:
        assert resolve_tts_source_format(object()) == LEGACY_PIPELINE_TTS_FORMAT
        assert resolve_tts_source_format(None) == LEGACY_PIPELINE_TTS_FORMAT

    def test_passthrough_detection(self) -> None:
        mulaw = parse_wire_format_string("ulaw_8000")
        assert mulaw is not None
        assert formats_match(mulaw, CARRIER_WIRE_FORMAT["twilio"]) is True
        pcm16 = parse_wire_format_string("pcm_16000")
        assert pcm16 is not None
        assert formats_match(pcm16, CARRIER_WIRE_FORMAT["twilio"]) is False
