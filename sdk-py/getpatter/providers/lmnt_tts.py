# Portions of this file are adapted from LiveKit Agents (Apache License 2.0):
#   https://github.com/livekit/agents
#   livekit-plugins/livekit-plugins-lmnt/livekit/plugins/lmnt/tts.py
#   Source commit: 78a66bcf79c5cea82989401c408f1dff4b961a5b
#
# Copyright 2023 LiveKit, Inc.
# Modifications (c) 2025 PatterAI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""LMNT TTS provider — HTTP bytes endpoint, pure aiohttp.

LMNT supports ``aac``, ``mp3``, ``mulaw``, ``raw`` and ``wav`` outputs. This
port defaults to ``raw`` (PCM_S16LE) at 16000 Hz so the output integrates
with Patter's telephony pipeline without transcoding.
"""

from __future__ import annotations

import os
from typing import Any, AsyncIterator, Literal, Optional

from getpatter.providers.base import TTSProvider

try:  # pragma: no cover - trivial import guard
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore

LMNT_BASE_URL = "https://api.lmnt.com/v1/ai/speech/bytes"

LMNTAudioFormats = Literal["aac", "mp3", "mulaw", "raw", "wav"]
LMNTModels = Literal["blizzard", "aurora"]
LMNTSampleRate = Literal[8000, 16000, 24000]


class LMNTTTS(TTSProvider):
    """LMNT TTS over the HTTP ``/v1/ai/speech/bytes`` endpoint.

    Default output is 16 kHz PCM_S16LE (``format='raw'``) which matches the
    Patter pipeline's standard telephony sample rate.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        model: LMNTModels = "blizzard",
        voice: str = "leah",
        language: Optional[str] = None,
        format: LMNTAudioFormats = "raw",
        sample_rate: LMNTSampleRate = 16000,
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

        # Mirror the upstream language defaults.
        if language is None:
            language = "auto" if model == "blizzard" else "en"

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

    def _record_synthesis_cost(self, text: str) -> None:
        from getpatter.observability.attributes import record_patter_attrs

        record_patter_attrs(
            {
                "patter.cost.tts_chars": len(text),
                "patter.tts.provider": "lmnt",
            }
        )

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Stream audio bytes for ``text``.

        With the default ``format='raw'`` these are PCM_S16LE chunks at the
        configured ``sample_rate``.
        """
        self._record_synthesis_cost(text)
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
