"""Regression tests for the public `getpatter.tts` facade modules.

The facade classes (``getpatter.tts.elevenlabs.TTS``, …) wrap the lower-level
provider adapters and are what users construct in pipeline mode. They must
forward the language / locale kwarg downward — when the facade signature is
narrower than the provider, multilingual scenarios silently lose accent
support.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _stub_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")


@pytest.mark.unit
def test_elevenlabs_facade_forwards_language_code() -> None:
    """``elevenlabs.TTS(language_code='it')`` must reach the provider."""
    from getpatter.tts import elevenlabs as eleven

    tts = eleven.TTS(language_code="it")
    assert tts.language_code == "it"


@pytest.mark.unit
def test_elevenlabs_facade_forwards_voice_settings() -> None:
    settings = {"stability": 0.4, "similarity_boost": 0.7}
    from getpatter.tts import elevenlabs as eleven

    tts = eleven.TTS(voice_settings=settings)
    assert tts.voice_settings == settings


@pytest.mark.unit
def test_elevenlabs_facade_defaults_keep_provider_defaults() -> None:
    """Backward-compat: omitting the new kwargs leaves the provider defaults."""
    from getpatter.tts import elevenlabs as eleven

    tts = eleven.TTS()
    assert tts.language_code is None
    assert tts.voice_settings is None
    assert tts.chunk_size == 4096


@pytest.mark.unit
def test_elevenlabs_facade_for_twilio_keeps_optional_kwargs_default() -> None:
    """The carrier factories were not touched by the language fix — still
    work with their original 3-kwarg shape."""
    from getpatter.tts import elevenlabs as eleven

    tts = eleven.TTS.for_twilio()
    assert tts.output_format == "ulaw_8000"
    assert tts.language_code is None


@pytest.mark.unit
def test_elevenlabs_facade_resolves_api_key_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", "env-key")
    from getpatter.tts import elevenlabs as eleven

    tts = eleven.TTS()
    # The facade must not have swallowed the env-resolved key.
    assert tts.api_key == "env-key"


@pytest.mark.unit
def test_elevenlabs_facade_explicit_api_key_wins() -> None:
    from getpatter.tts import elevenlabs as eleven

    tts = eleven.TTS(api_key="explicit-key")
    assert tts.api_key == "explicit-key"


@pytest.mark.unit
def test_elevenlabs_facade_missing_api_key_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    from getpatter.tts import elevenlabs as eleven

    with pytest.raises(ValueError, match="ELEVENLABS_API_KEY"):
        eleven.TTS()


# Mirror module so the importorskip works under all CI extras combinations.
def _facade_path() -> str:  # pragma: no cover — used only for skip decorators
    return os.path.join(os.path.dirname(__file__), "..", "tts", "elevenlabs.py")
