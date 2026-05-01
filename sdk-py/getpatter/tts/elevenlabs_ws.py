"""ElevenLabs WebSocket TTS for Patter pipeline mode (opt-in low-latency)."""

from __future__ import annotations

import os
from typing import ClassVar

from getpatter.providers.elevenlabs_ws_tts import (
    ElevenLabsWebSocketTTS as _ElevenLabsWebSocketTTS,
)

__all__ = ["TTS"]


def _resolve_api_key(api_key: str | None) -> str:
    key = api_key or os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        raise ValueError(
            "ElevenLabs WebSocket TTS requires an api_key. Pass api_key='...' "
            "or set ELEVENLABS_API_KEY in the environment."
        )
    return key


class TTS(_ElevenLabsWebSocketTTS):
    """ElevenLabs streaming TTS over WebSocket (``stream-input`` endpoint).

    Drop-in replacement for :class:`getpatter.tts.elevenlabs.TTS` (HTTP)
    that uses the WebSocket transport. Saves the per-utterance HTTP request
    setup time; otherwise behaves identically.

    Example::

        from getpatter.tts import elevenlabs_ws

        tts = elevenlabs_ws.TTS()              # reads ELEVENLABS_API_KEY
        tts = elevenlabs_ws.TTS(api_key="...", voice_id="EXAVITQu4vr4xnSDxMaL")

    Telephony optimization
    ----------------------
    Use :meth:`for_twilio` (μ-law @ 8 kHz, native Twilio Media Streams
    format) or :meth:`for_telnyx` (PCM @ 16 kHz) — same wire-format
    optimisation as the HTTP variant.
    """

    provider_key: ClassVar[str] = "elevenlabs_ws"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        voice_id: str | None = None,
        model_id: str = "eleven_flash_v2_5",
        output_format: str = "pcm_16000",
        auto_mode: bool = True,
        voice_settings: dict | None = None,
        language_code: str | None = None,
        inactivity_timeout: int | None = None,
        chunk_length_schedule: list[int] | None = None,
    ) -> None:
        # ``voice_id`` defaults are owned by the provider class so the public
        # wrapper and the low-level class agree on the default voice. Pass
        # only when the caller specifies one.
        kwargs: dict = {
            "api_key": _resolve_api_key(api_key),
            "model_id": model_id,
            "output_format": output_format,
            "auto_mode": auto_mode,
        }
        if voice_id is not None:
            kwargs["voice_id"] = voice_id
        if voice_settings is not None:
            kwargs["voice_settings"] = voice_settings
        if language_code is not None:
            kwargs["language_code"] = language_code
        if inactivity_timeout is not None:
            kwargs["inactivity_timeout"] = inactivity_timeout
        if chunk_length_schedule is not None:
            kwargs["chunk_length_schedule"] = chunk_length_schedule
        super().__init__(**kwargs)

    @classmethod
    def for_twilio(
        cls,
        api_key: str | None = None,
        *,
        voice_id: str | None = None,
        model_id: str = "eleven_flash_v2_5",
        auto_mode: bool = True,
        voice_settings: dict | None = None,
        language_code: str | None = None,
        inactivity_timeout: int | None = None,
    ) -> "TTS":
        """WebSocket TTS pre-configured for Twilio Media Streams (``ulaw_8000``)."""
        return cls(
            api_key=api_key,
            voice_id=voice_id,
            model_id=model_id,
            output_format="ulaw_8000",
            auto_mode=auto_mode,
            voice_settings=voice_settings,
            language_code=language_code,
            inactivity_timeout=inactivity_timeout,
        )

    @classmethod
    def for_telnyx(
        cls,
        api_key: str | None = None,
        *,
        voice_id: str | None = None,
        model_id: str = "eleven_flash_v2_5",
        auto_mode: bool = True,
        voice_settings: dict | None = None,
        language_code: str | None = None,
        inactivity_timeout: int | None = None,
    ) -> "TTS":
        """WebSocket TTS pre-configured for Telnyx (``pcm_16000``)."""
        return cls(
            api_key=api_key,
            voice_id=voice_id,
            model_id=model_id,
            output_format="pcm_16000",
            auto_mode=auto_mode,
            voice_settings=voice_settings,
            language_code=language_code,
            inactivity_timeout=inactivity_timeout,
        )
