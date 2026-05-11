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
    """Backward-compat: omitting the new kwargs leaves the provider defaults.

    Since 0.6.1 the ``elevenlabs.TTS`` facade defaults to the WebSocket
    streaming transport (provider_key ``"elevenlabs_ws"``). The WS provider
    does not chunk on the client side — chunking is driven by
    ``chunk_length_schedule`` server-side — so ``chunk_size`` is no longer
    a meaningful attribute. The kwarg remains accepted as a no-op for
    backward compatibility with REST-era call sites; see
    ``test_elevenlabs_rest_tts_preserves_chunk_size_default`` for the
    explicit-REST equivalent of the legacy assertion.
    """
    from getpatter.tts import elevenlabs as eleven

    tts = eleven.TTS()
    assert tts.language_code is None
    assert tts.voice_settings is None
    # WS transport is the new default — flip recorded by ``provider_key``.
    assert tts.provider_key == "elevenlabs_ws"
    # The historical REST ``chunk_size`` kwarg is still accepted (no-op) so
    # existing user code does not break.
    tts_with_chunk = eleven.TTS(chunk_size=4096)
    assert tts_with_chunk.provider_key == "elevenlabs_ws"


@pytest.mark.unit
def test_elevenlabs_rest_tts_preserves_chunk_size_default() -> None:
    """Explicit REST opt-out keeps the historical ``chunk_size=4096`` default.

    Users on the HTTP REST transport (``ElevenLabsRestTTS``) still drive
    chunking client-side, so the attribute must remain available and the
    default unchanged.
    """
    from getpatter import ElevenLabsRestTTS

    tts = ElevenLabsRestTTS(api_key="test-key")
    assert tts.chunk_size == 4096


@pytest.mark.unit
def test_elevenlabs_facade_returns_websocket_provider() -> None:
    """The facade now defaults to the WebSocket adapter, not REST."""
    from getpatter import ElevenLabsRestTTS
    from getpatter.providers.elevenlabs_ws_tts import ElevenLabsWebSocketTTS
    from getpatter.tts import elevenlabs as eleven

    tts = eleven.TTS()
    assert isinstance(tts, ElevenLabsWebSocketTTS)
    # And conversely, ``ElevenLabsRestTTS`` is not aliased to the WS class.
    assert ElevenLabsRestTTS is not ElevenLabsWebSocketTTS


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
