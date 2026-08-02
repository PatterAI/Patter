"""Fish Audio TTS for Patter pipeline mode (Beta)."""

from __future__ import annotations

import os
from typing import Any, ClassVar, Optional, Sequence, Union

from getpatter.providers.fish_audio_tts import (
    FISH_AUDIO_DEFAULT_SAMPLE_RATE,
    FishAudioTTS as _FishAudioTTS,
)
from getpatter.providers.fish_audio_ws_tts import (
    FishAudioWebSocketTTS as _FishAudioWebSocketTTS,
)

__all__ = ["TTS", "WebSocketTTS"]


def _resolve_api_key(api_key: str | None) -> str:
    key = api_key or os.environ.get("FISH_AUDIO_API_KEY")
    if not key:
        raise ValueError(
            "Fish Audio TTS requires an api_key. Pass api_key='...' or "
            "set FISH_AUDIO_API_KEY in the environment."
        )
    return key


class TTS(_FishAudioTTS):
    """Fish Audio HTTP TTS — defaults to the ``s2.1-pro`` model (Beta).

    Example::

        from getpatter.tts import fish_audio

        tts = fish_audio.TTS()                                  # reads FISH_AUDIO_API_KEY
        tts = fish_audio.TTS(api_key="...", voice="<reference-id>", latency="low")

    ``voice`` is a Fish ``reference_id`` — a voice-model id from the Fish voice
    library or one you cloned. Omit it to use the model's built-in voice; pass a
    sequence of ids for multi-speaker synthesis with ``<|speaker:0|>`` markers.

    Telephony: use :meth:`for_twilio` / :meth:`for_telnyx` on phone calls so the
    PCM is requested at the carrier's rate and the pipeline skips a resample.
    """

    provider_key: ClassVar[str] = "fish_audio"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = "s2.1-pro",
        voice: Optional[Union[str, Sequence[str]]] = None,
        format: str = "pcm",
        sample_rate: int = FISH_AUDIO_DEFAULT_SAMPLE_RATE,
        latency: str = "balanced",
        **kwargs: Any,
    ) -> None:
        super().__init__(
            api_key=_resolve_api_key(api_key),
            model=model,
            voice=voice,
            format=format,
            sample_rate=sample_rate,
            latency=latency,
            **kwargs,
        )


class WebSocketTTS(_FishAudioWebSocketTTS):
    """Fish Audio WebSocket TTS — opt-in low-latency path, ``s2-pro`` (Beta).

    Example::

        from getpatter.tts import fish_audio

        tts = fish_audio.WebSocketTTS()                         # reads FISH_AUDIO_API_KEY

    Fish serves only ``s1`` and ``s2-pro`` on the streaming socket — for
    ``s2.1-pro`` use :class:`TTS` (HTTP) instead.
    """

    provider_key: ClassVar[str] = "fish_audio"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = "s2-pro",
        voice: Optional[Union[str, Sequence[str]]] = None,
        format: str = "pcm",
        sample_rate: int = FISH_AUDIO_DEFAULT_SAMPLE_RATE,
        latency: str = "balanced",
        **kwargs: Any,
    ) -> None:
        super().__init__(
            api_key=_resolve_api_key(api_key),
            model=model,
            voice=voice,
            format=format,
            sample_rate=sample_rate,
            latency=latency,
            **kwargs,
        )
