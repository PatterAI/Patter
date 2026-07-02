"""Soniox TTS provider — HTTP one-shot bytes endpoint, pure aiohttp.

Targets the REST ``POST https://tts-rt.soniox.com/tts`` endpoint which returns
raw audio bytes (Content-Type matches the requested ``audio_format``). This
maps cleanly onto Patter's ``TTSProvider.synthesize(text) -> AsyncIterator[bytes]``
contract and requires no vendor SDK — only ``aiohttp``.

Soniox also exposes a richer WebSocket streaming mode
(``wss://tts-rt.soniox.com/tts-websocket``) that starts emitting audio from
the first few words for lower TTFB; that mode is not used here because the REST
endpoint already maps onto Patter's streaming contract while keeping the
dependency / state surface minimal (mirrors how :class:`CartesiaTTS` opts for
the HTTP bytes path over Cartesia's WebSocket variant).

Credential: Soniox TTS shares the ``SONIOX_API_KEY`` env var / key family with
the existing Soniox STT integration (host ``*.soniox.com``). REST auth is an
``Authorization: Bearer <key>`` header.

Telephony: Soniox natively supports G.711 mu-law @ 8 kHz — request
``audio_format="pcm_mulaw"`` + ``sample_rate=8000`` (the :meth:`for_twilio` /
:meth:`for_telnyx` factories do this) to emit carrier-native audio with NO
resampling, exactly matching Patter's mu-law/8 kHz wire format.
"""

from __future__ import annotations

import logging
import os
from enum import IntEnum, StrEnum
from typing import ClassVar, Any, AsyncIterator, Optional, Union

from getpatter.providers.base import TTSProvider

logger = logging.getLogger("getpatter.providers.soniox_tts")

# Lazy import: aiohttp is declared as an optional dep for this provider
# (shared ``[soniox]`` extra with the Soniox STT adapter).
try:  # pragma: no cover - trivial import guard
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore

# REST one-shot text-to-speech endpoint. Returns raw audio bytes.
SONIOX_TTS_REST_URL = "https://tts-rt.soniox.com/tts"
# WebSocket streaming endpoint (documented for reference; not used by this
# HTTP-bytes adapter).
SONIOX_TTS_WS_URL = "wss://tts-rt.soniox.com/tts-websocket"

# Soniox's default voice. A single voice keeps its timbre across 60+
# languages and supports seamless mid-sentence language switching.
SONIOX_DEFAULT_VOICE = "Adrian"


class SonioxTTSModel(StrEnum):
    """Soniox real-time TTS model identifiers."""

    TTS_RT_V1 = "tts-rt-v1"
    # Deprecated alias retained for back-compat — now points at tts-rt-v1.
    TTS_RT_V1_PREVIEW = "tts-rt-v1-preview"


class SonioxTTSAudioFormat(StrEnum):
    """Output encodings accepted by Soniox TTS ``audio_format``.

    ``PCM_MULAW`` / ``PCM_ALAW`` @ 8 kHz are the carrier-native G.711 codecs
    (telephony passthrough); the remaining formats target web/dashboard use.
    """

    PCM_S16LE = "pcm_s16le"
    PCM_F32LE = "pcm_f32le"
    PCM_MULAW = "pcm_mulaw"
    PCM_ALAW = "pcm_alaw"
    WAV = "wav"
    MP3 = "mp3"
    AAC = "aac"
    OPUS = "opus"
    FLAC = "flac"


class SonioxTTSSampleRate(IntEnum):
    """Sample rates (Hz) accepted by Soniox TTS ``sample_rate``."""

    HZ_8000 = 8000
    HZ_16000 = 16000
    HZ_24000 = 24000
    HZ_44100 = 44100
    HZ_48000 = 48000


class SonioxTTS(TTSProvider):
    """Soniox TTS over the HTTP one-shot ``/tts`` bytes endpoint.

    Default output is PCM_S16LE at 16 kHz so it drops into Patter's pipeline
    without transcoding. Default model is ``tts-rt-v1`` and default voice is
    ``Adrian``.

    Telephony optimization
    ----------------------
    The constructor default ``sample_rate=16000`` / ``audio_format="pcm_s16le"``
    is correct for web playback, dashboard previews, and 16 kHz pipelines. For
    real phone calls use the carrier factories instead:

    * :meth:`for_twilio` — emits ``pcm_mulaw`` @ 8 kHz, Twilio's exact wire
      codec, so the pipeline skips resampling AND PCM -> mu-law encoding
      (bit-clean passthrough). The sender reads the declared output format via
      :meth:`source_audio_format`.
    * :meth:`for_telnyx` — emits ``pcm_mulaw`` @ 8 kHz. The SDK pins the Telnyx
      wire to PCMU/mu-law @ 8 kHz, so this flows end-to-end with zero
      resampling — same passthrough win as the Twilio factory.
    """

    #: Stable pricing/dashboard key — read by stream-handler/metrics.
    provider_key: ClassVar[str] = "soniox_tts"

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        model: Union[SonioxTTSModel, str] = SonioxTTSModel.TTS_RT_V1,
        voice: str = SONIOX_DEFAULT_VOICE,
        language: str = "en",
        audio_format: Union[SonioxTTSAudioFormat, str] = SonioxTTSAudioFormat.PCM_S16LE,
        sample_rate: Union[SonioxTTSSampleRate, int] = SonioxTTSSampleRate.HZ_16000,
        bitrate: Optional[int] = None,
        speed: Optional[float] = None,
        base_url: str = SONIOX_TTS_REST_URL,
        session: Optional["aiohttp.ClientSession"] = None,
    ) -> None:
        if aiohttp is None:
            raise ImportError(
                "aiohttp is required for SonioxTTS. "
                "Install with: pip install getpatter[soniox]"
            )

        resolved_key = api_key or os.environ.get("SONIOX_API_KEY")
        if not resolved_key:
            raise ValueError(
                "Soniox API key is required, either as argument or set "
                "SONIOX_API_KEY environment variable"
            )

        self.api_key = resolved_key
        self.model = model
        self.voice = voice
        self.language = language
        self.audio_format = audio_format
        self.sample_rate = sample_rate
        self.bitrate = bitrate
        self.speed = speed
        self.base_url = base_url
        self._owns_session = session is None
        self._session = session

    def __repr__(self) -> str:
        # Never leak the API key in repr / logs.
        return (
            f"SonioxTTS(model={self.model!r}, voice={self.voice!r}, "
            f"language={self.language!r}, audio_format={self.audio_format!r}, "
            f"sample_rate={self.sample_rate})"
        )

    # ------------------------------------------------------------------
    # Telephony factories
    # ------------------------------------------------------------------

    @classmethod
    def for_twilio(
        cls,
        api_key: Optional[str] = None,
        **kwargs: Any,
    ) -> "SonioxTTS":
        """Build an instance pre-configured for Twilio Media Streams.

        Emits ``pcm_mulaw`` @ 8 kHz — exactly Twilio's wire codec — so the
        pipeline takes the passthrough path: zero resampling, zero PCM ->
        mu-law encoding, bit-clean audio. Soniox supports mu-law @ 8 kHz
        natively, so this is a true carrier-native synthesis (no downsampling
        from a PCM-only model).
        """
        kwargs.pop("sample_rate", None)
        kwargs.pop("audio_format", None)
        return cls(
            api_key=api_key,
            audio_format=SonioxTTSAudioFormat.PCM_MULAW,
            sample_rate=SonioxTTSSampleRate.HZ_8000,
            **kwargs,
        )

    @classmethod
    def for_telnyx(
        cls,
        api_key: Optional[str] = None,
        **kwargs: Any,
    ) -> "SonioxTTS":
        """Build an instance pre-configured for Telnyx bidirectional media.

        The SDK pins the Telnyx wire to PCMU/mu-law @ 8 kHz, so emitting
        ``pcm_mulaw`` @ 8 kHz flows end-to-end with zero resampling or
        transcoding — the same passthrough win as :meth:`for_twilio`.
        """
        kwargs.pop("sample_rate", None)
        kwargs.pop("audio_format", None)
        return cls(
            api_key=api_key,
            audio_format=SonioxTTSAudioFormat.PCM_MULAW,
            sample_rate=SonioxTTSSampleRate.HZ_8000,
            **kwargs,
        )

    def source_audio_format(self) -> "AudioFormat":
        """Declare the audio format this adapter emits, so the pipeline sender
        derives the correct resample ratio (or skips it for mu-law passthrough)
        instead of assuming a fixed 16 kHz source. See ``getpatter.audio.format``.
        """
        from getpatter.audio.format import AudioFormat

        fmt = str(self.audio_format)
        if fmt == SonioxTTSAudioFormat.PCM_MULAW.value:
            return AudioFormat(encoding="mulaw", sample_rate=int(self.sample_rate))
        if fmt == SonioxTTSAudioFormat.PCM_ALAW.value:
            return AudioFormat(encoding="alaw", sample_rate=int(self.sample_rate))
        return AudioFormat(encoding="pcm_s16le", sample_rate=int(self.sample_rate))

    def _ensure_session(self) -> "aiohttp.ClientSession":
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    def _build_payload(self, text: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": str(self.model),
            "text": text,
            "voice": self.voice,
            "language": self.language,
            "audio_format": str(self.audio_format),
            "sample_rate": int(self.sample_rate),
        }
        if self.bitrate is not None:
            payload["bitrate"] = self.bitrate
        if self.speed is not None:
            payload["speed"] = self.speed
        return payload

    def _record_synthesis_cost(self, text: str) -> None:
        """Emit ``patter.cost.tts_chars`` for the synthesised text."""
        try:
            from getpatter.observability.attributes import record_patter_attrs

            record_patter_attrs(
                {
                    "patter.cost.tts_chars": len(text),
                    "patter.tts.provider": "soniox_tts",
                }
            )
        except Exception:  # pragma: no cover — defense in depth
            logger.debug("_record_synthesis_cost failed", exc_info=True)

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Stream raw audio bytes for ``text`` over HTTP.

        With the default ``audio_format="pcm_s16le"`` these are raw PCM_S16LE
        chunks at the configured ``sample_rate``; with ``pcm_mulaw`` (see
        :meth:`for_twilio`) they are carrier-native G.711 mu-law bytes.
        """
        self._record_synthesis_cost(text)
        session = self._ensure_session()

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        async with session.post(
            self.base_url,
            headers=headers,
            json=self._build_payload(text),
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"Soniox TTS error {resp.status}: {body[:500]}")
            async for chunk in resp.content.iter_chunked(4096):
                if chunk:
                    yield chunk

    async def close(self) -> None:
        """Close the underlying session (idempotent)."""
        if self._session is not None and self._owns_session:
            await self._session.close()
            self._session = None
