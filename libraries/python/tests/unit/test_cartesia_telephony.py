"""Unit tests for Cartesia telephony format factories.

Verifies that ``CartesiaTTS.for_twilio`` / ``for_telnyx`` and the
pipeline-mode ``getpatter.tts.cartesia.TTS`` analogues construct instances
that request ``pcm_mulaw`` @ 8 kHz — the carrier wire codec — so the pipeline
takes the bit-clean passthrough path (no resample, no PCM → μ-law encode).
The sender reads the declared format via ``source_audio_format()``.
"""

from __future__ import annotations

import pytest

from getpatter.providers.cartesia_tts import CartesiaTTS
from getpatter.tts.cartesia import TTS as PipelineCartesiaTTS


@pytest.mark.unit
class TestProviderTelephonyFactories:
    """Provider-level factories on the low-level adapter class."""

    def test_for_twilio_emits_mulaw_8k_natively(self) -> None:
        tts = CartesiaTTS.for_twilio(api_key="x")
        assert tts.sample_rate == 8000
        assert tts.encoding == "pcm_mulaw"

    def test_for_twilio_payload_requests_mulaw_8k(self) -> None:
        tts = CartesiaTTS.for_twilio(api_key="x")
        payload = tts._build_payload("hello")
        assert payload["output_format"]["sample_rate"] == 8000
        # μ-law @ 8 kHz IS the Twilio wire codec → carrier-native passthrough.
        assert payload["output_format"]["encoding"] == "pcm_mulaw"

    def test_for_twilio_declares_mulaw_source_format(self) -> None:
        from getpatter.audio.format import AudioFormat

        tts = CartesiaTTS.for_twilio(api_key="x")
        assert tts.source_audio_format() == AudioFormat(
            encoding="mulaw", sample_rate=8000
        )

    def test_for_twilio_respects_overrides(self) -> None:
        tts = CartesiaTTS.for_twilio(
            api_key="x",
            voice="custom-voice-id",
            language="es",
            speed="fast",
        )
        assert tts.sample_rate == 8000
        assert tts.encoding == "pcm_mulaw"
        assert tts.voice == "custom-voice-id"
        assert tts.language == "es"
        assert tts.speed == "fast"

    def test_for_twilio_ignores_caller_sample_rate(self) -> None:
        # Even if the caller passes sample_rate, the factory pins the μ-law
        # 8 kHz carrier wire so the passthrough contract is preserved.
        tts = CartesiaTTS.for_twilio(api_key="x", sample_rate=24000)
        assert tts.sample_rate == 8000

    def test_for_telnyx_emits_mulaw_8k_natively(self) -> None:
        tts = CartesiaTTS.for_telnyx(api_key="x")
        assert tts.sample_rate == 8000
        assert tts.encoding == "pcm_mulaw"

    def test_for_telnyx_ignores_caller_sample_rate(self) -> None:
        tts = CartesiaTTS.for_telnyx(api_key="x", sample_rate=24000)
        assert tts.sample_rate == 8000

    def test_constructor_default_unchanged(self) -> None:
        """Non-telephony users keep getting 16 kHz PCM from the bare constructor."""
        tts = CartesiaTTS(api_key="x")
        assert tts.sample_rate == 16000
        assert tts.encoding == "pcm_s16le"


@pytest.mark.unit
class TestPipelineTTSTelephonyFactories:
    """Same factories on the pipeline-mode ``TTS`` wrapper."""

    def test_for_twilio_emits_mulaw_8k(self) -> None:
        tts = PipelineCartesiaTTS.for_twilio(api_key="x")
        assert tts.sample_rate == 8000
        assert tts.encoding == "pcm_mulaw"

    def test_for_telnyx_emits_mulaw_8k(self) -> None:
        tts = PipelineCartesiaTTS.for_telnyx(api_key="x")
        assert tts.sample_rate == 8000
        assert tts.encoding == "pcm_mulaw"

    def test_for_twilio_requires_api_key_or_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CARTESIA_API_KEY", raising=False)
        with pytest.raises(ValueError, match="CARTESIA_API_KEY"):
            PipelineCartesiaTTS.for_twilio()

    def test_for_twilio_uses_env_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CARTESIA_API_KEY", "env-key")
        tts = PipelineCartesiaTTS.for_twilio()
        assert tts.sample_rate == 8000

    def test_for_telnyx_uses_env_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CARTESIA_API_KEY", "env-key")
        tts = PipelineCartesiaTTS.for_telnyx()
        assert tts.sample_rate == 8000


@pytest.mark.unit
class TestTopLevelImportSmoke:
    """Smoke test: ``from getpatter import CartesiaTTS`` resolves the pipeline wrapper."""

    def test_top_level_for_twilio(self) -> None:
        from getpatter import CartesiaTTS as PipelineWrapper

        tts = PipelineWrapper.for_twilio(api_key="x")
        assert tts.sample_rate == 8000
        assert tts.encoding == "pcm_mulaw"
