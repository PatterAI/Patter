# Portions of this file are adapted from LiveKit Agents (Apache License 2.0):
#   https://github.com/livekit/agents
#   livekit-plugins/livekit-plugins-cartesia/livekit/plugins/cartesia/tts.py
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

"""Cartesia TTS provider — HTTP bytes endpoint, pure aiohttp.

The upstream LiveKit Agents plugin also supports a WebSocket streaming mode
with word timestamps, sentence tokenization, and connection pooling. This
port focuses on the chunked-bytes HTTP API which maps cleanly to Patter's
``TTSProvider.synthesize(text) -> AsyncIterator[bytes]`` contract and
requires no vendor SDK.
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
CARTESIA_API_VERSION = "2024-11-13"

# Cartesia's "Katie — Friendly Fixer" is the LiveKit default.
CARTESIA_DEFAULT_VOICE_ID = "f786b574-daa5-4673-aa0c-cbe3e8534c02"

TTSEncoding = Literal["pcm_s16le"]
TTSVoiceSpeed = Literal["fastest", "fast", "normal", "slow", "slowest"]


class CartesiaTTS(TTSProvider):
    """Cartesia TTS over the HTTP ``/tts/bytes`` endpoint.

    Output is PCM_S16LE at the configured sample rate (default 16000 Hz so it
    lines up with Patter's telephony pipeline without a resample step).
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        model: str = "sonic-2",
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
