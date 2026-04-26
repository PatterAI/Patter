# Portions of this file are adapted from LiveKit Agents (Apache License 2.0):
#   https://github.com/livekit/agents
#   livekit-plugins/livekit-plugins-rime/livekit/plugins/rime/tts.py
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

"""Rime TTS provider — HTTP chunked PCM endpoint, pure aiohttp.

Both Arcana and Mist model families are supported. Requests Rime's
``audio/pcm`` accept header so chunks are raw PCM_S16LE suitable for direct
use in Patter's pipeline.
"""

from __future__ import annotations

import os
from typing import Any, AsyncIterator, Optional

from getpatter.providers.base import TTSProvider

try:  # pragma: no cover - trivial import guard
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore

RIME_BASE_URL = "https://users.rime.ai/v1/rime-tts"

# Model-specific timeouts — Arcana can take up to ~80% of the audio duration
# it is synthesizing.
ARCANA_MODEL_TIMEOUT = 60 * 4
MIST_MODEL_TIMEOUT = 30


def _is_mist_model(model: str) -> bool:
    # Rime Mist-family model ids are ``mist``, ``mistv2``, etc. — always
    # prefixed with ``mist``. Use ``startswith`` so unrelated model ids that
    # happen to contain the substring don't accidentally match.
    return model.startswith("mist")


def _timeout_for_model(model: str) -> int:
    if model == "arcana":
        return ARCANA_MODEL_TIMEOUT
    return MIST_MODEL_TIMEOUT


class RimeTTS(TTSProvider):
    """Rime TTS over the HTTP chunked endpoint.

    Defaults to the ``arcana`` model with the ``astra`` voice. Output is
    PCM_S16LE at the configured ``sample_rate`` (default 16000 Hz).
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        model: str = "arcana",
        speaker: Optional[str] = None,
        lang: str = "eng",
        sample_rate: int = 16000,
        # Arcana-only options
        repetition_penalty: Optional[float] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
        # Mist-only options
        speed_alpha: Optional[float] = None,
        reduce_latency: Optional[bool] = None,
        pause_between_brackets: Optional[bool] = None,
        phonemize_between_brackets: Optional[bool] = None,
        base_url: str = RIME_BASE_URL,
        session: Optional["aiohttp.ClientSession"] = None,
    ) -> None:
        if aiohttp is None:
            raise ImportError(
                "aiohttp is required for RimeTTS. "
                "Install with: pip install getpatter[rime]"
            )

        resolved_key = api_key or os.environ.get("RIME_API_KEY")
        if not resolved_key:
            raise ValueError(
                "Rime API key is required, either as argument or set "
                "RIME_API_KEY environment variable"
            )

        if speaker is None:
            # Upstream LiveKit uses "cove" (DefaultMistVoice) for Mist models
            # and "astra" for Arcana. We mirror the Arcana default here and
            # let callers override for Mist.
            speaker = "cove" if _is_mist_model(model) else "astra"

        self.api_key = resolved_key
        self.model = model
        self.speaker = speaker
        self.lang = lang
        self.sample_rate = sample_rate
        self.repetition_penalty = repetition_penalty
        self.temperature = temperature
        self.top_p = top_p
        self.max_tokens = max_tokens
        self.speed_alpha = speed_alpha
        self.reduce_latency = reduce_latency
        self.pause_between_brackets = pause_between_brackets
        self.phonemize_between_brackets = phonemize_between_brackets
        self.base_url = base_url
        self._total_timeout = _timeout_for_model(model)
        self._owns_session = session is None
        self._session = session

    def __repr__(self) -> str:
        return (
            f"RimeTTS(model={self.model!r}, speaker={self.speaker!r}, "
            f"lang={self.lang!r}, sample_rate={self.sample_rate})"
        )

    def _ensure_session(self) -> "aiohttp.ClientSession":
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    def _build_payload(self, text: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "speaker": self.speaker,
            "text": text,
            "modelId": self.model,
        }

        if self.model == "arcana":
            if self.repetition_penalty is not None:
                payload["repetition_penalty"] = self.repetition_penalty
            if self.temperature is not None:
                payload["temperature"] = self.temperature
            if self.top_p is not None:
                payload["top_p"] = self.top_p
            if self.max_tokens is not None:
                payload["max_tokens"] = self.max_tokens
            payload["lang"] = self.lang
            payload["samplingRate"] = self.sample_rate
        elif _is_mist_model(self.model):
            payload["lang"] = self.lang
            payload["samplingRate"] = self.sample_rate
            if self.speed_alpha is not None:
                payload["speedAlpha"] = self.speed_alpha
            if self.model == "mistv2" and self.reduce_latency is not None:
                payload["reduceLatency"] = self.reduce_latency
            if self.pause_between_brackets is not None:
                payload["pauseBetweenBrackets"] = self.pause_between_brackets
            if self.phonemize_between_brackets is not None:
                payload["phonemizeBetweenBrackets"] = self.phonemize_between_brackets

        return payload

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Stream raw PCM_S16LE bytes for ``text`` over HTTP."""
        session = self._ensure_session()
        accept = "audio/pcm"

        headers = {
            "accept": accept,
            "Authorization": f"Bearer {self.api_key}",
            "content-type": "application/json",
        }

        async with session.post(
            self.base_url,
            headers=headers,
            json=self._build_payload(text),
            timeout=aiohttp.ClientTimeout(total=self._total_timeout),
        ) as resp:
            resp.raise_for_status()
            # Rime returns audio/pcm on success; any other content type is a
            # surface-level error (e.g. JSON error payload).
            content_type = resp.headers.get("Content-Type", "")
            if not content_type.startswith("audio"):
                body = await resp.text()
                raise RuntimeError(f"Rime returned non-audio response: {body[:500]}")

            async for chunk in resp.content.iter_chunked(4096):
                if chunk:
                    yield chunk

    async def close(self) -> None:
        """Close the underlying session (idempotent)."""
        if self._session is not None and self._owns_session:
            await self._session.close()
            self._session = None
