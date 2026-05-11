"""ElevenLabs TTS for Patter pipeline mode.

Default transport is **WebSocket streaming** (``stream-input`` endpoint),
which removes the per-utterance HTTP request setup time of the legacy
REST variant. For callers that need the HTTP REST transport explicitly
(simpler retries, no persistent socket), import
:class:`getpatter.ElevenLabsRestTTS` instead.
"""

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
            "ElevenLabs TTS requires an api_key. Pass api_key='...' or "
            "set ELEVENLABS_API_KEY in the environment."
        )
    return key


class TTS(_ElevenLabsWebSocketTTS):
    """ElevenLabs streaming TTS.

    Default = WebSocket streaming (added 0.6.1). For HTTP REST opt-out:
    use ``ElevenLabsRestTTS(...)`` directly.

    Example::

        from getpatter.tts import elevenlabs

        tts = elevenlabs.TTS()              # reads ELEVENLABS_API_KEY
        tts = elevenlabs.TTS(api_key="...", voice_id="EXAVITQu4vr4xnSDxMaL")

    Telephony optimization
    ----------------------
    Use :meth:`for_twilio` (μ-law @ 8 kHz, native Twilio Media Streams
    format) or :meth:`for_telnyx` (PCM @ 16 kHz, native Telnyx default)
    to skip the SDK-side resampling / transcoding step on phone calls.
    """

    provider_key: ClassVar[str] = "elevenlabs_ws"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        voice_id: str = "EXAVITQu4vr4xnSDxMaL",
        model_id: str = "eleven_flash_v2_5",
        output_format: str = "pcm_16000",
        language_code: str | None = None,
        voice_settings: dict | None = None,
        auto_mode: bool = True,
        inactivity_timeout: int | None = None,
        chunk_length_schedule: list[int] | None = None,
        # ``chunk_size`` is accepted for backward compatibility with the
        # historical REST-backed signature but ignored by the WS transport
        # (chunking is driven by ``chunk_length_schedule`` on that path).
        chunk_size: int | None = None,
    ) -> None:
        kwargs: dict = {
            "api_key": _resolve_api_key(api_key),
            "voice_id": voice_id,
            "model_id": model_id,
            "output_format": output_format,
            "auto_mode": auto_mode,
        }
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
        voice_id: str = "EXAVITQu4vr4xnSDxMaL",
        model_id: str = "eleven_flash_v2_5",
    ) -> "TTS":
        """Pipeline TTS pre-configured for Twilio Media Streams (``ulaw_8000``).

        Falls back to ``ELEVENLABS_API_KEY`` from the env when ``api_key``
        is omitted.
        """
        return cls(
            api_key=_resolve_api_key(api_key),
            voice_id=voice_id,
            model_id=model_id,
            output_format="ulaw_8000",
        )

    @classmethod
    def for_telnyx(
        cls,
        api_key: str | None = None,
        *,
        voice_id: str = "EXAVITQu4vr4xnSDxMaL",
        model_id: str = "eleven_flash_v2_5",
    ) -> "TTS":
        """Pipeline TTS pre-configured for Telnyx (``pcm_16000``).

        Falls back to ``ELEVENLABS_API_KEY`` from the env when ``api_key``
        is omitted.
        """
        return cls(
            api_key=_resolve_api_key(api_key),
            voice_id=voice_id,
            model_id=model_id,
            output_format="pcm_16000",
        )
