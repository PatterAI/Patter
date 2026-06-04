"""Unit tests for GeminiLive engine dataclass and _unpack_engine dispatch."""

from __future__ import annotations

import pytest


@pytest.mark.unit
class TestGeminiLiveEngine:
    def test_defaults(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-fake-key")
        from getpatter.engines.gemini_live import GeminiLive

        engine = GeminiLive()
        assert engine.voice == "Puck"
        assert engine.model == "gemini-2.5-flash-native-audio-preview-09-2025"
        assert engine.input_sample_rate == 16000
        assert engine.output_sample_rate == 24000
        assert engine.response_modalities == ("AUDIO",)

    def test_custom_params(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-fake-key")
        from getpatter.engines.gemini_live import GeminiLive

        engine = GeminiLive(voice="Kore", model="gemini-custom-model-v1")
        assert engine.voice == "Kore"
        assert engine.model == "gemini-custom-model-v1"
        assert engine.input_sample_rate == 16000
        assert engine.output_sample_rate == 24000

    def test_missing_key_raises(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        from getpatter.engines.gemini_live import GeminiLive

        with pytest.raises(ValueError, match="Gemini Live"):
            GeminiLive()

    def test_unpack_engine(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-fake-key")
        from getpatter.client import Patter
        from getpatter.engines.gemini_live import GeminiLive

        engine = GeminiLive(
            api_key="explicit-key-999",
            voice="Aoede",
            model="gemini-2.5-flash-native-audio-preview-09-2025",
        )
        kind, fields = Patter._unpack_engine(engine)

        assert kind == "gemini_live"
        assert fields["api_key"] == "explicit-key-999"
        assert fields["voice"] == "Aoede"
        assert fields["model"] == "gemini-2.5-flash-native-audio-preview-09-2025"
        assert fields["input_sample_rate"] == 16000
        assert fields["output_sample_rate"] == 24000
