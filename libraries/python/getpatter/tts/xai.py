"""xAI (Grok) TTS for Patter pipeline mode."""

from __future__ import annotations

import os
from typing import ClassVar, Literal

from getpatter.providers.xai_tts import XAITTS as _XAITTS

__all__ = ["TTS"]


class TTS(_XAITTS):
    """xAI TTS (WebSocket streaming by default; ``eve`` voice).

    Example::

        from getpatter.tts import xai

        tts = xai.TTS()                              # reads XAI_API_KEY
        tts = xai.TTS(api_key="xai-...", voice="ara", language="auto")

    **Telephony** — use :meth:`TTS.for_twilio` / :meth:`TTS.for_telnyx` on
    phone calls. Both emit G.711 μ-law @ 8 kHz natively, so the pipeline
    skips resampling and PCM → μ-law encoding entirely.
    """

    provider_key: ClassVar[str] = "xai_tts"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        voice: str = "eve",
        language: str = "en",
        codec: str = "pcm",
        sample_rate: int = 16000,
        bit_rate: int | None = None,
        speed: float | None = None,
        text_normalization: bool = False,
        optimize_streaming_latency: int = 1,
        transport: Literal["websocket", "http"] = "websocket",
    ) -> None:
        key = api_key or os.environ.get("XAI_API_KEY")
        if not key:
            raise ValueError(
                "xAI TTS requires an api_key. Pass api_key='xai-...' or "
                "set XAI_API_KEY in the environment."
            )
        super().__init__(
            api_key=key,
            voice=voice,
            language=language,
            codec=codec,
            sample_rate=sample_rate,
            bit_rate=bit_rate,
            speed=speed,
            text_normalization=text_normalization,
            optimize_streaming_latency=optimize_streaming_latency,
            transport=transport,
        )
