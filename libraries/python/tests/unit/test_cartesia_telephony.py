"""Unit tests for Cartesia telephony sample-rate factories.

Verifies that ``CartesiaTTS.for_twilio`` / ``for_telnyx`` and the
pipeline-mode ``getpatter.tts.cartesia.TTS`` analogues construct
instances that request the carrier-native sample rate from the Cartesia
``/tts/bytes`` endpoint. Asking Cartesia for 8 kHz directly skips the
16 kHz → 8 kHz resample step the SDK would otherwise perform before
PCM → μ-law transcoding for Twilio.
"""

from __future__ import annotations

import pytest

from getpatter.providers.cartesia_tts import CartesiaTTS
from getpatter.tts.cartesia import TTS as PipelineCartesiaTTS


@pytest.mark.unit
class TestProviderTelephonyFactories:
    """Provider-level factories on the low-level adapter class."""

    def test_for_twilio_uses_8000_hz(self) -> None:
        tts = CartesiaTTS.for_twilio(api_key="x")
        assert tts.sample_rate == 8000

    def test_for_twilio_payload_requests_8000_hz(self) -> None:
        tts = CartesiaTTS.for_twilio(api_key="x")
        payload = tts._build_payload("hello")
        assert payload["output_format"]["sample_rate"] == 8000
        # Encoding stays PCM_S16LE — μ-law transcoding still happens
        # client-side in TwilioAudioSender.
        assert payload["output_format"]["encoding"] == "pcm_s16le"

    def test_for_twilio_respects_overrides(self) -> None:
        tts = CartesiaTTS.for_twilio(
            api_key="x",
            voice="custom-voice-id",
            language="es",
            speed="fast",
        )
        assert tts.sample_rate == 8000
        assert tts.voice == "custom-voice-id"
        assert tts.language == "es"
        assert tts.speed == "fast"

    def test_for_twilio_ignores_caller_sample_rate(self) -> None:
        # Even if the caller passes sample_rate, the factory wins so the
        # contract ("for_twilio == 8000 Hz") is preserved.
        tts = CartesiaTTS.for_twilio(api_key="x", sample_rate=24000)
        assert tts.sample_rate == 8000

    def test_for_telnyx_uses_16000_hz(self) -> None:
        tts = CartesiaTTS.for_telnyx(api_key="x")
        assert tts.sample_rate == 16000

    def test_for_telnyx_ignores_caller_sample_rate(self) -> None:
        tts = CartesiaTTS.for_telnyx(api_key="x", sample_rate=24000)
        assert tts.sample_rate == 16000

    def test_constructor_default_unchanged(self) -> None:
        """Non-telephony users keep getting 16 kHz from the bare constructor."""
        tts = CartesiaTTS(api_key="x")
        assert tts.sample_rate == 16000


@pytest.mark.unit
class TestPipelineTTSTelephonyFactories:
    """Same factories on the pipeline-mode ``TTS`` wrapper."""

    def test_for_twilio_uses_8000_hz(self) -> None:
        tts = PipelineCartesiaTTS.for_twilio(api_key="x")
        assert tts.sample_rate == 8000

    def test_for_telnyx_uses_16000_hz(self) -> None:
        tts = PipelineCartesiaTTS.for_telnyx(api_key="x")
        assert tts.sample_rate == 16000

    def test_for_twilio_requires_api_key_or_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CARTESIA_API_KEY", raising=False)
        with pytest.raises(ValueError, match="CARTESIA_API_KEY"):
            PipelineCartesiaTTS.for_twilio()

    def test_for_twilio_uses_env_api_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CARTESIA_API_KEY", "env-key")
        tts = PipelineCartesiaTTS.for_twilio()
        assert tts.sample_rate == 8000

    def test_for_telnyx_uses_env_api_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CARTESIA_API_KEY", "env-key")
        tts = PipelineCartesiaTTS.for_telnyx()
        assert tts.sample_rate == 16000


@pytest.mark.unit
class TestTopLevelImportSmoke:
    """Smoke test: ``from getpatter import CartesiaTTS`` resolves the pipeline wrapper."""

    def test_top_level_for_twilio(self) -> None:
        from getpatter import CartesiaTTS as PipelineWrapper

        tts = PipelineWrapper.for_twilio(api_key="x")
        assert tts.sample_rate == 8000
