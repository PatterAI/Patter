"""LMNT TTS provider — HTTP bytes endpoint, pure aiohttp.

LMNT supports ``aac``, ``mp3``, ``mulaw``, ``raw`` and ``wav`` outputs. This
implementation defaults to ``raw`` (PCM_S16LE) at 16000 Hz so the output
integrates with Patter's telephony pipeline without transcoding.
"""

from __future__ import annotations

import os
from enum import IntEnum, StrEnum
from typing import Any, AsyncIterator, Optional

from getpatter.providers.base import TTSProvider

try:  # pragma: no cover - trivial import guard
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore

LMNT_BASE_URL = "https://api.lmnt.com/v1/ai/speech/bytes"


class LMNTAudioFormat(StrEnum):
    """Supported LMNT audio output formats. ``RAW`` is PCM_S16LE."""

    AAC = "aac"
    MP3 = "mp3"
    MULAW = "mulaw"
    RAW = "raw"
    WAV = "wav"


class LMNTModel(StrEnum):
    """LMNT TTS model families."""

    BLIZZARD = "blizzard"
    AURORA = "aurora"


class LMNTSampleRate(IntEnum):
    """Supported PCM sample rates for LMNT raw output."""

    HZ_8000 = 8000
    HZ_16000 = 16000
    HZ_24000 = 24000


# Backwards-compat aliases (existing imports keep working).
LMNTAudioFormats = LMNTAudioFormat
LMNTModels = LMNTModel


class LMNTTTS(TTSProvider):
    """LMNT TTS over the HTTP ``/v1/ai/speech/bytes`` endpoint.

    Default output is 16 kHz PCM_S16LE (``format='raw'``) which matches the
    Patter pipeline's standard telephony sample rate.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        model: Union[LMNTModel, str] = LMNTModel.BLIZZARD,
        voice: str = "leah",
        language: Optional[str] = None,
        format: Union[LMNTAudioFormat, str] = LMNTAudioFormat.RAW,
        sample_rate: Union[LMNTSampleRate, int] = LMNTSampleRate.HZ_16000,
        temperature: float = 1.0,
        top_p: float = 0.8,
        base_url: str = LMNT_BASE_URL,
        session: Optional["aiohttp.ClientSession"] = None,
    ) -> None:
        if aiohttp is None:
            raise ImportError(
                "aiohttp is required for LMNTTTS. "
                "Install with: pip install getpatter[lmnt]"
            )

        resolved_key = api_key or os.environ.get("LMNT_API_KEY")
        if not resolved_key:
            raise ValueError(
                "LMNT API key is required, either as argument or set "
                "LMNT_API_KEY environment variable"
            )

        # Per-model language defaults.
        if language is None:
            language = "auto" if model == LMNTModel.BLIZZARD.value else "en"

        self.api_key = resolved_key
        self.model = model
        self.voice = voice
        self.language = language
        self.format = format
        self.sample_rate = sample_rate
        self.temperature = temperature
        self.top_p = top_p
        self.base_url = base_url
        self._owns_session = session is None
        self._session = session

    def __repr__(self) -> str:
        return (
            f"LMNTTTS(model={self.model!r}, voice={self.voice!r}, "
            f"language={self.language!r}, format={self.format!r}, "
            f"sample_rate={self.sample_rate})"
        )

    def _ensure_session(self) -> "aiohttp.ClientSession":
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    def _build_payload(self, text: str) -> dict[str, Any]:
        return {
            "text": text,
            "voice": self.voice,
            "language": self.language,
            "sample_rate": self.sample_rate,
            "model": self.model,
            "format": self.format,
            "temperature": self.temperature,
            "top_p": self.top_p,
        }

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Stream audio bytes for ``text``.

        With the default ``format='raw'`` these are PCM_S16LE chunks at the
        configured ``sample_rate``.
        """
        session = self._ensure_session()

        headers = {
            "Content-Type": "application/json",
            "X-API-Key": self.api_key,
        }

        async with session.post(
            self.base_url,
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
