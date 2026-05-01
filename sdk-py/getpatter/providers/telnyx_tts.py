"""Telnyx Text-to-Speech provider (WebSocket streaming).

Bridges the Telnyx ``/v2/text-to-speech/speech`` WebSocket API to Patter's
:class:`~getpatter.providers.base.TTSProvider` interface. The server returns
MP3-encoded audio; we yield raw MP3 bytes to the caller. Downstream Patter
code that expects PCM should pipe through a decoder (e.g. ``ffmpeg`` /
``pydub``).
"""

from __future__ import annotations

import base64
import json
from typing import AsyncIterator

import aiohttp

from getpatter.providers.base import TTSProvider

TELNYX_TTS_WS_URL = "wss://api.telnyx.com/v2/text-to-speech/speech"

DEFAULT_VOICE = "Telnyx.NaturalHD.astra"
DEFAULT_SAMPLE_RATE = 16000
NUM_CHANNELS = 1


class TelnyxTTS(TTSProvider):
    """Streaming TTS adapter backed by Telnyx ``/v2/text-to-speech/speech``.

    Args:
        api_key: Telnyx API key (Bearer token).
        voice: Telnyx voice ID (e.g. ``"Telnyx.NaturalHD.astra"``).
        base_url: Override the base WebSocket URL (for testing).
        session: Optional pre-built ``aiohttp.ClientSession``. If omitted, a new
            session is created and closed with :meth:`close`.
    """

    def __init__(
        self,
        api_key: str,
        voice: str = DEFAULT_VOICE,
        *,
        base_url: str = TELNYX_TTS_WS_URL,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self.api_key = api_key
        self.voice = voice
        self.base_url = base_url

        self._session = session
        self._owns_session = session is None

    def __repr__(self) -> str:
        return f"TelnyxTTS(voice={self.voice!r})"

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Synthesise *text* and yield MP3-encoded audio chunks.

        The Telnyx TTS WebSocket returns each chunk wrapped in a JSON frame
        ``{"audio": "<base64-encoded mp3 bytes>"}``. Upstream callers typically
        decode these through an MP3 decoder before feeding the telephony
        WebSocket.
        """
        session = self._ensure_session()
        url = f"{self.base_url}?voice={self.voice}"
        headers = {"Authorization": f"Bearer {self.api_key}"}

        ws = await session.ws_connect(url, headers=headers)
        try:
            # Protocol: send empty warm-up frame, then the text, then terminator.
            await ws.send_str(json.dumps({"text": " "}))
            await ws.send_str(json.dumps({"text": text}))
            await ws.send_str(json.dumps({"text": ""}))

            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except (ValueError, TypeError):
                        continue
                    audio_b64 = data.get("audio")
                    if not audio_b64:
                        continue
                    try:
                        audio_bytes = base64.b64decode(audio_b64)
                    except (ValueError, TypeError):
                        continue
                    if audio_bytes:
                        yield audio_bytes
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                ):
                    break
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    break
        finally:
            try:
                await ws.close()
            except Exception:
                pass

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None
