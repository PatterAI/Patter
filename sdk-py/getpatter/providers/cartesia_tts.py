"""Cartesia TTS provider — HTTP bytes endpoint, pure aiohttp.

Targets the chunked-bytes HTTP API which maps cleanly to Patter's
``TTSProvider.synthesize(text) -> AsyncIterator[bytes]`` contract and
requires no vendor SDK. Cartesia also exposes a richer WebSocket streaming
mode with word timestamps and sentence tokenization; that mode is not used
here because the HTTP API already meets Patter's TTFB target while keeping
the dependency surface minimal.
"""

from __future__ import annotations

import os
from typing import Any, AsyncIterator, Literal, Optional

from getpatter.providers.base import TTSProvider

# Lazy import: aiohttp is declared as an optional dep for this provider.
try:  # pragma: no cover - trivial import guard
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore

CARTESIA_BASE_URL = "https://api.cartesia.ai"
# Cartesia API version pin — kept in sync with our STT integration and the
# Cartesia Line skill. ``2025-04-16`` is the current GA snapshot.
CARTESIA_API_VERSION = "2025-04-16"

# Cartesia's "Katie — Friendly Fixer" is a sensible default. The voice ID
# is stable across the sonic-2 / sonic-3 model bump.
CARTESIA_DEFAULT_VOICE_ID = "f786b574-daa5-4673-aa0c-cbe3e8534c02"

TTSEncoding = Literal["pcm_s16le"]
TTSVoiceSpeed = Literal["fastest", "fast", "normal", "slow", "slowest"]


class CartesiaTTS(TTSProvider):
    """Cartesia TTS over the HTTP ``/tts/bytes`` endpoint.

    Output is PCM_S16LE at the configured sample rate (default 16000 Hz so it
    lines up with Patter's telephony pipeline without a resample step).

    Default model is ``sonic-3`` (GA snapshot ``sonic-3-2026-01-12``) — the
    current Cartesia GA model, with a documented ~90 ms TTFB target. The
    sonic-2 voice IDs (e.g. the default Katie voice) remain compatible on
    sonic-3 so the upgrade is drop-in.

    Telephony optimization
    ----------------------
    The constructor default ``sample_rate=16000`` is correct for web
    playback, dashboard previews, and 16 kHz pipelines. For real phone
    calls use the carrier-specific factories instead:

    * :meth:`for_twilio` — requests ``sample_rate=8000`` natively from
      Cartesia. Twilio's media-stream WebSocket expects μ-law @ 8 kHz, so
      the SDK normally resamples 16 kHz → 8 kHz before doing the PCM →
      μ-law transcode in ``TwilioAudioSender``. Asking Cartesia for
      8 kHz PCM at the source skips the resample step (saves ~10–30 ms
      first-byte plus per-frame CPU and removes a potential aliasing
      source). The PCM → μ-law transcode still happens client-side.
    * :meth:`for_telnyx` — requests ``sample_rate=16000``. Telnyx
      negotiates L16/16000 on its bidirectional media WebSocket, so
      16 kHz PCM is already the format used end-to-end and no
      transcoding happens. This is the same as the bare constructor
      default and exists for API symmetry with the Twilio factory.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        model: str = "sonic-3",
        voice: str = CARTESIA_DEFAULT_VOICE_ID,
        language: str = "en",
        sample_rate: int = 16000,
        speed: Optional[str | float] = None,
        emotion: Optional[str | list[str]] = None,
        volume: Optional[float] = None,
        base_url: str = CARTESIA_BASE_URL,
        api_version: str = CARTESIA_API_VERSION,
        session: Optional["aiohttp.ClientSession"] = None,
    ) -> None:
        if aiohttp is None:
            raise ImportError(
                "aiohttp is required for CartesiaTTS. "
                "Install with: pip install getpatter[cartesia]"
            )

        resolved_key = api_key or os.environ.get("CARTESIA_API_KEY")
        if not resolved_key:
            raise ValueError(
                "Cartesia API key is required, either as argument or set "
                "CARTESIA_API_KEY environment variable"
            )

        self.api_key = resolved_key
        self.model = model
        self.voice = voice
        self.language = language
        self.sample_rate = sample_rate
        self.speed = speed
        self.emotion = [emotion] if isinstance(emotion, str) else emotion
        self.volume = volume
        self.base_url = base_url
        self.api_version = api_version
        self._owns_session = session is None
        self._session = session

    def __repr__(self) -> str:
        # Never leak the API key in repr / logs.
        return (
            f"CartesiaTTS(model={self.model!r}, voice={self.voice!r}, "
            f"language={self.language!r}, sample_rate={self.sample_rate})"
        )

    # ------------------------------------------------------------------
    # Telephony factories
    # ------------------------------------------------------------------

    @classmethod
    def for_twilio(
        cls,
        api_key: Optional[str] = None,
        **kwargs: Any,
    ) -> "CartesiaTTS":
        """Build an instance pre-configured for Twilio Media Streams.

        Sets ``sample_rate=8000`` so Cartesia emits PCM_S16LE @ 8 kHz
        directly. Twilio's media stream uses μ-law @ 8 kHz so the SDK
        still does the PCM → μ-law transcode client-side, but the
        16 kHz → 8 kHz resample step is skipped. Saves ~10–30 ms first-
        byte plus per-frame CPU and removes a potential aliasing source.
        """
        kwargs.pop("sample_rate", None)
        return cls(api_key=api_key, sample_rate=8000, **kwargs)

    @classmethod
    def for_telnyx(
        cls,
        api_key: Optional[str] = None,
        **kwargs: Any,
    ) -> "CartesiaTTS":
        """Build an instance pre-configured for Telnyx bidirectional media.

        Sets ``sample_rate=16000`` to match Telnyx's L16/16000 default
        codec — audio flows end-to-end with zero resampling or
        transcoding. This is the same as the bare-constructor default
        and exists for API symmetry with :meth:`for_twilio`.
        """
        kwargs.pop("sample_rate", None)
        return cls(api_key=api_key, sample_rate=16000, **kwargs)

    def _ensure_session(self) -> "aiohttp.ClientSession":
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    def _build_payload(self, text: str) -> dict[str, Any]:
        voice: dict[str, Any] = {"mode": "id", "id": self.voice}

        payload: dict[str, Any] = {
            "model_id": self.model,
            "voice": voice,
            "transcript": text,
            "output_format": {
                "container": "raw",
                "encoding": "pcm_s16le",
                "sample_rate": self.sample_rate,
            },
            "language": self.language,
        }

        generation_config: dict[str, Any] = {}
        if self.speed is not None:
            generation_config["speed"] = self.speed
        if self.emotion:
            generation_config["emotion"] = self.emotion[0]
        if self.volume is not None:
            generation_config["volume"] = self.volume
        if generation_config:
            payload["generation_config"] = generation_config

        return payload

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Stream raw PCM_S16LE bytes for ``text`` over HTTP."""
        session = self._ensure_session()

        headers = {
            "X-API-Key": self.api_key,
            "Cartesia-Version": self.api_version,
            "Content-Type": "application/json",
        }

        async with session.post(
            f"{self.base_url}/tts/bytes",
            headers=headers,
            json=self._build_payload(text),
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            resp.raise_for_status()
            async for chunk in resp.content.iter_chunked(4096):
                if chunk:
                    yield chunk

    async def close(self) -> None:
        """Close the underlying session (idempotent)."""
        if self._session is not None and self._owns_session:
            await self._session.close()
            self._session = None
