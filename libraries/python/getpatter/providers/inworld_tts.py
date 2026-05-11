"""Inworld TTS provider — HTTP NDJSON streaming endpoint, pure aiohttp.

Calls ``POST https://api.inworld.ai/tts/v1/voice:stream``. The response is
NDJSON: one JSON object per line of the form
``{"result": {"audioContent": "<base64-PCM_S16LE>", "timestampInfo": ...}}``.

Default config requests ``audioEncoding=PCM`` at 16 kHz so the output drops
straight into the Patter pipeline without transcoding. Inworld TTS-2 is the
default model — pass ``model="inworld-tts-1.5-max"`` for the prior generation.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from enum import StrEnum
from typing import ClassVar, Any, AsyncIterator, Optional, Union

from getpatter.providers.base import TTSProvider

logger = logging.getLogger("getpatter.providers.inworld_tts")

try:  # pragma: no cover - trivial import guard
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore

INWORLD_BASE_URL = "https://api.inworld.ai/tts/v1/voice:stream"
# Voice metadata endpoint used as a billing-safe warmup target. The
# streaming endpoint above is POST-only so HEAD against it returns 405.
# ``GET /tts/v1/voices`` is documented as a free metadata read that
# returns the configured voice catalogue without invoking the synthesis
# pipeline (per https://docs.inworld.ai/).
INWORLD_VOICES_URL = "https://api.inworld.ai/tts/v1/voices"


class InworldModel(StrEnum):
    """Inworld TTS model families."""

    TTS_2 = "inworld-tts-2"
    TTS_1_5_MAX = "inworld-tts-1.5-max"
    TTS_1_5_MINI = "inworld-tts-1.5-mini"
    TTS_1_MAX = "inworld-tts-1-max"
    TTS_1 = "inworld-tts-1"


class InworldAudioEncoding(StrEnum):
    """Audio encoding values accepted by the REST API."""

    PCM = "PCM"
    LINEAR16 = "LINEAR16"
    OGG_OPUS = "OGG_OPUS"
    MP3 = "MP3"


class InworldDeliveryMode(StrEnum):
    """TTS-2 stability mode (ignored by older models)."""

    EXPRESSIVE = "EXPRESSIVE"
    BALANCED = "BALANCED"
    STABLE = "STABLE"


class InworldTTS(TTSProvider):
    """Inworld TTS over the ``/tts/v1/voice:stream`` HTTP NDJSON endpoint.

    The Inworld dashboard provides a Base64 token that is already in the form
    expected by the ``Authorization: Basic <token>`` header — pass it as-is.
    If you only have the raw API key string, base64-encode ``"<api_key>:"``
    yourself before calling the constructor.
    """

    #: Stable pricing/dashboard key — read by stream-handler/metrics.
    provider_key: ClassVar[str] = "inworld"

    def __init__(
        self,
        auth_token: Optional[str] = None,
        *,
        model: Union[InworldModel, str] = InworldModel.TTS_2,
        voice: str = "Ashley",
        language: Optional[str] = None,
        audio_encoding: Union[InworldAudioEncoding, str] = InworldAudioEncoding.PCM,
        sample_rate: int = 16000,
        bitrate: int = 64000,
        temperature: Optional[float] = None,
        speaking_rate: float = 1.0,
        delivery_mode: Optional[Union[InworldDeliveryMode, str]] = None,
        base_url: str = INWORLD_BASE_URL,
        session: Optional["aiohttp.ClientSession"] = None,
    ) -> None:
        if aiohttp is None:
            raise ImportError(
                "aiohttp is required for InworldTTS. "
                "Install with: pip install getpatter[inworld]"
            )

        resolved_token = auth_token or os.environ.get("INWORLD_API_KEY")
        if not resolved_token:
            raise ValueError(
                "Inworld TTS requires an auth_token. Pass auth_token='...' or "
                "set INWORLD_API_KEY in the environment."
            )

        self.auth_token = resolved_token
        self.model = model
        self.voice = voice
        self.language = language
        self.audio_encoding = audio_encoding
        self.sample_rate = sample_rate
        self.bitrate = bitrate
        self.temperature = temperature
        self.speaking_rate = speaking_rate
        self.delivery_mode = delivery_mode
        self.base_url = base_url
        self._owns_session = session is None
        self._session = session

    def __repr__(self) -> str:
        return (
            f"InworldTTS(model={self.model!r}, voice={self.voice!r}, "
            f"audio_encoding={self.audio_encoding!r}, sample_rate={self.sample_rate})"
        )

    def _ensure_session(self) -> "aiohttp.ClientSession":
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    def _build_payload(self, text: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "text": text,
            "voiceId": self.voice,
            "modelId": str(self.model),
            "audioConfig": {
                "audioEncoding": str(self.audio_encoding),
                "bitrate": self.bitrate,
                "sampleRateHertz": self.sample_rate,
            },
            "speakingRate": self.speaking_rate,
        }
        if self.language is not None:
            payload["language"] = self.language
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.delivery_mode is not None:
            payload["deliveryMode"] = str(self.delivery_mode)
        return payload

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Stream audio bytes for ``text``.

        With the default ``audio_encoding=PCM`` these are raw PCM_S16LE
        chunks at ``sample_rate`` Hz.
        """
        session = self._ensure_session()

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Basic {self.auth_token}",
        }

        async with session.post(
            self.base_url,
            headers=headers,
            json=self._build_payload(text),
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"Inworld TTS error {resp.status}: {body[:500]}")
            # NDJSON: one JSON object per line. ``aiohttp`` exposes the
            # streaming body as an async iterator of lines via
            # ``resp.content``; ``readline`` keeps memory bounded for long
            # responses.
            async for raw_line in resp.content:
                line = raw_line.strip()
                if not line:
                    continue
                audio = _decode_ndjson_line(line)
                if audio:
                    yield audio

    async def warmup(self) -> None:
        """Pre-call HTTP warmup for the Inworld TTS API.

        Issues a lightweight ``GET /tts/v1/voices`` against the API host
        so DNS + TLS + HTTP/2 connection are already up by the time the
        first :meth:`synthesize` POST lands. Best-effort: 5 s timeout,
        all exceptions swallowed at DEBUG.

        Earlier revisions issued ``HEAD`` against the streaming endpoint
        (``/tts/v1/voice:stream``). That endpoint is POST-only so HEAD
        returns ``405 Method Not Allowed`` — the warmup still completed
        the TLS handshake but spammed 405 errors into Inworld's audit
        logs and into our own logs. Switching to a documented
        ``GET /tts/v1/voices`` metadata read is a 2xx-clean equivalent.

        Billing safety: ``GET /tts/v1/voices`` is a free metadata
        endpoint (per https://docs.inworld.ai/). It returns the voice
        catalogue without invoking the synthesis pipeline. The actual
        synthesis is billed only when ``POST /tts/v1/voice:stream`` runs
        with a non-empty ``text``.

        Note: Inworld TTS uses the HTTP NDJSON streaming path rather than
        a persistent WebSocket — connection warmup is therefore HTTP-based,
        not WebSocket pre-handshake. The latency win is smaller (~50-150 ms)
        than the WS-based prewarms but still real on cold-start calls.
        """
        try:
            session = self._ensure_session()
            headers = {"Authorization": f"Basic {self.auth_token}"}
            # ``GET /tts/v1/voices`` is a billing-safe metadata read that
            # returns 2xx (unlike HEAD against the POST-only streaming
            # endpoint, which returns 405).
            async with session.get(
                INWORLD_VOICES_URL,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                # Drain so the underlying connection returns cleanly to the pool.
                await resp.read()
        except Exception as exc:  # noqa: BLE001 - best-effort
            logger.debug("Inworld TTS warmup failed (best-effort): %s", exc)

    async def close(self) -> None:
        """Close the underlying session (idempotent)."""
        if self._session is not None and self._owns_session:
            await self._session.close()
            self._session = None


def _decode_ndjson_line(line: bytes) -> Optional[bytes]:
    """Decode one NDJSON line. Returns ``None`` for lines without audio."""
    try:
        parsed = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    result = parsed.get("result")
    if not isinstance(result, dict):
        return None
    audio_b64 = result.get("audioContent")
    if not isinstance(audio_b64, str) or not audio_b64:
        return None
    try:
        return base64.b64decode(audio_b64)
    except (ValueError, TypeError):
        return None
