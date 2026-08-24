"""Unit tests for the xAI built-in voice catalog (roster + normalization).

Pure data + pure functions here — there is no network boundary to mock, so
every assertion below runs against real code. See
``.claude/rules/authentic-tests.md``.
"""

from __future__ import annotations

import dataclasses

import pytest

from getpatter.models import STTConfig, TTSConfig
from getpatter.providers import xai, xai_asr
from getpatter.providers.xai_voices import (
    XAI_DEFAULT_VOICE,
    XAI_VOICE_IDS,
    XAI_VOICES,
    XaiVoice,
    get_xai_voice,
    is_xai_builtin_voice,
    normalize_xai_voice,
)


@pytest.mark.unit
class TestRoster:
    def test_roster_has_26_voices(self) -> None:
        assert len(XAI_VOICES) == 26
        assert len(XAI_VOICE_IDS) == 26

    def test_eve_is_default_and_first(self) -> None:
        assert XAI_DEFAULT_VOICE == "eve"
        assert XAI_VOICES[0].id == "eve"
        assert XAI_VOICE_IDS[0] == "eve"

    def test_all_ids_are_lowercase_and_unique(self) -> None:
        assert all(voice_id == voice_id.lower() for voice_id in XAI_VOICE_IDS)
        assert len(set(XAI_VOICE_IDS)) == len(XAI_VOICE_IDS)

    def test_voice_ids_match_roster_order(self) -> None:
        assert XAI_VOICE_IDS == tuple(v.id for v in XAI_VOICES)

    def test_voices_are_frozen(self) -> None:
        voice = XAI_VOICES[0]
        assert isinstance(voice, XaiVoice)
        with pytest.raises(dataclasses.FrozenInstanceError):
            voice.id = "someone-else"  # type: ignore[misc]


@pytest.mark.unit
class TestIsBuiltinVoice:
    def test_true_for_builtin_id_case_insensitive(self) -> None:
        assert is_xai_builtin_voice("EVE") is True
        assert is_xai_builtin_voice("eve") is True
        assert is_xai_builtin_voice(" Eve ") is True

    def test_false_for_custom_voice_id(self) -> None:
        assert is_xai_builtin_voice("my-custom-id") is False


@pytest.mark.unit
class TestNormalizeXaiVoice:
    def test_builtin_id_is_trimmed_and_lowercased(self) -> None:
        assert normalize_xai_voice(" Leo ") == "leo"

    def test_custom_id_is_left_untouched(self) -> None:
        assert normalize_xai_voice("CustomVoice_1") == "CustomVoice_1"

    def test_custom_id_surrounding_whitespace_is_trimmed_only(self) -> None:
        assert normalize_xai_voice("  CustomVoice_1  ") == "CustomVoice_1"


@pytest.mark.unit
class TestGetXaiVoice:
    def test_returns_roster_metadata_for_builtin_id(self) -> None:
        voice = get_xai_voice("carina")
        assert voice is not None
        assert "Wellness" in voice.use_cases

    def test_is_case_insensitive_and_trims(self) -> None:
        assert get_xai_voice("CARINA") == get_xai_voice(" carina ")

    def test_returns_none_for_custom_voice_id(self) -> None:
        assert get_xai_voice("my-custom-id") is None


@pytest.mark.unit
class TestConfigHelpers:
    def test_xai_returns_tts_config_with_default_voice(self) -> None:
        config = xai("xai-test-key")
        assert isinstance(config, TTSConfig)
        assert config.provider == "xai_tts"
        assert config.api_key == "xai-test-key"
        assert config.voice == "eve"

    def test_xai_accepts_explicit_voice(self) -> None:
        config = xai("xai-test-key", voice="ara")
        assert config.voice == "ara"

    def test_xai_asr_returns_stt_config_with_default_language(self) -> None:
        config = xai_asr("xai-test-key")
        assert isinstance(config, STTConfig)
        assert config.provider == "xai"
        assert config.api_key == "xai-test-key"
        assert config.language == "en"

    def test_xai_asr_accepts_explicit_language(self) -> None:
        config = xai_asr("xai-test-key", language="es")
        assert config.language == "es"
