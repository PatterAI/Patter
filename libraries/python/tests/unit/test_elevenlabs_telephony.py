"""Unit tests for ElevenLabs telephony codec factories.

Verifies that ``ElevenLabsTTS.for_twilio`` / ``for_telnyx`` and the
pipeline-mode ``getpatter.tts.elevenlabs.TTS`` analogues construct
instances with the right native output format. Picking the correct
carrier-native codec at the source lets the SDK skip the 16 kHz → 8 kHz
resample and PCM ↔ μ-law transcoding that ``TwilioAudioSender`` would
otherwise perform — saving ~30–80 ms first-byte plus per-frame CPU.
"""

from __future__ import annotations

import pytest

from getpatter.providers.elevenlabs_tts import ElevenLabsTTS
from getpatter.tts.elevenlabs import TTS as PipelineElevenLabsTTS


@pytest.mark.unit
class TestProviderTelephonyFactories:
    """Provider-level factories on the low-level adapter class."""

    def test_for_twilio_emits_ulaw_8000(self) -> None:
        tts = ElevenLabsTTS.for_twilio(api_key="x")
        assert tts.output_format == "ulaw_8000"
        # Default sensible voice settings for narrowband μ-law.
        assert tts.voice_settings is not None
        assert tts.voice_settings["use_speaker_boost"] is False

    def test_for_twilio_respects_overrides(self) -> None:
        custom_settings = {"stability": 0.4, "use_speaker_boost": True}
        tts = ElevenLabsTTS.for_twilio(
            api_key="x",
            voice_id="rachel",
            model_id="eleven_v3",
            voice_settings=custom_settings,
            language_code="en",
        )
        assert tts.output_format == "ulaw_8000"
        assert tts.model_id == "eleven_v3"
        # voice_id is normalized via resolve_voice_id ("rachel" → opaque ID).
        assert tts.voice_id == "21m00Tcm4TlvDq8ikWAM"
        assert tts.voice_settings is custom_settings
        assert tts.language_code == "en"

    def test_for_telnyx_emits_pcm_16000(self) -> None:
        tts = ElevenLabsTTS.for_telnyx(api_key="x")
        assert tts.output_format == "pcm_16000"

    def test_constructor_default_unchanged(self) -> None:
        """Non-telephony users keep getting pcm_16000 from the bare constructor."""
        tts = ElevenLabsTTS(api_key="x")
        assert tts.output_format == "pcm_16000"


@pytest.mark.unit
class TestPipelineTTSTelephonyFactories:
    """Same factories on the pipeline-mode ``TTS`` wrapper."""

    def test_for_twilio_emits_ulaw_8000(self) -> None:
        tts = PipelineElevenLabsTTS.for_twilio(api_key="x")
        assert tts.output_format == "ulaw_8000"

    def test_for_telnyx_emits_pcm_16000(self) -> None:
        tts = PipelineElevenLabsTTS.for_telnyx(api_key="x")
        assert tts.output_format == "pcm_16000"

    def test_for_twilio_requires_api_key_or_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
        with pytest.raises(ValueError, match="ELEVENLABS_API_KEY"):
            PipelineElevenLabsTTS.for_twilio()

    def test_for_twilio_uses_env_api_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ELEVENLABS_API_KEY", "env-key")
        tts = PipelineElevenLabsTTS.for_twilio()
        assert tts.output_format == "ulaw_8000"
