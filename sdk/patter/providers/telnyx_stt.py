"""Telnyx Speech-to-Text provider (WebSocket streaming).

Bridges the Telnyx `/v2/speech-to-text/transcription` WebSocket API to Patter's
:class:`~patter.providers.base.STTProvider` interface.

Algorithm and WebSocket protocol adapted from LiveKit Agents (Apache 2.0):
https://github.com/livekit/agents
Source: ``livekit-plugins/livekit-plugins-telnyx/livekit/plugins/telnyx/stt.py``
Commit SHA (ref=main): 78a66bcf79c5cea82989401c408f1dff4b961a5b

The source project is licensed under the Apache 2.0 license:
https://www.apache.org/licenses/LICENSE-2.0

Changes vs. upstream:
    - Replaced ``livekit.agents.stt.STT`` base class with Patter's ``STTProvider``
      abstract class.
    - Replaced ``SpeechStream`` / event channel plumbing with an
      ``AsyncIterator[Transcript]`` via :meth:`receive_transcripts`.
    - Dropped LiveKit-specific utilities (``AudioByteStream``,
      ``gracefully_cancel``, ``Plugin`` registry) in favour of plain ``asyncio``.
"""

from __future__ import annotations

import asyncio
import json
import struct
from typing import AsyncIterator, Literal

import aiohttp

from patter.providers.base import STTProvider, Transcript

TELNYX_STT_WS_URL = "wss://api.telnyx.com/v2/speech-to-text/transcription"

DEFAULT_SAMPLE_RATE = 16000
NUM_CHANNELS = 1

TranscriptionEngine = Literal["telnyx", "google", "deepgram", "azure"]


def _create_streaming_wav_header(sample_rate: int, num_channels: int) -> bytes:
    """Create a WAV header for streaming with maximum possible size.

    Adapted from LiveKit Agents (Apache 2.0).
    """
    bytes_per_sample = 2
    byte_rate = sample_rate * num_channels * bytes_per_sample
    block_align = num_channels * bytes_per_sample
    data_size = 0x7FFFFFFF
    file_size = 36 + data_size

    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        file_size,
        b"WAVE",
        b"fmt ",
        16,
        1,
        num_channels,
        sample_rate,
        byte_rate,
        block_align,
        16,
        b"data",
        data_size,
    )
    return header


class TelnyxSTT(STTProvider):
    """Streaming STT adapter backed by Telnyx ``/v2/speech-to-text/transcription``.

    Args:
        api_key: Telnyx API key (Bearer token).
        language: Language code (e.g. ``"en"``, ``"es"``).
        transcription_engine: One of ``"telnyx"``, ``"google"``, ``"deepgram"``,
            ``"azure"``. Defaults to ``"telnyx"``.
        sample_rate: PCM sample rate in Hz. Defaults to 16 000.
        base_url: Override the base WebSocket URL (for testing).
        session: Optional pre-built ``aiohttp.ClientSession``. If omitted, a new
            session is created and closed with :meth:`close`.
    """

    def __init__(
        self,
        api_key: str,
        language: str = "en",
        *,
        transcription_engine: TranscriptionEngine = "telnyx",
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        base_url: str = TELNYX_STT_WS_URL,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self.api_key = api_key
        self.language = language
        self.transcription_engine = transcription_engine
        self.sample_rate = sample_rate
        self.base_url = base_url

        self._session = session
        self._owns_session = session is None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._header_sent = False
        self._queue: asyncio.Queue[Transcript | None] = asyncio.Queue()
        self._recv_task: asyncio.Task[None] | None = None

    def __repr__(self) -> str:
        return (
            f"TelnyxSTT(engine={self.transcription_engine!r}, "
            f"language={self.language!r}, sample_rate={self.sample_rate})"
        )

    async def connect(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._owns_session = True

        params = {
            "transcription_engine": self.transcription_engine,
            "language": self.language,
            "input_format": "wav",
        }
        query_string = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{self.base_url}?{query_string}"
        headers = {"Authorization": f"Bearer {self.api_key}"}

        self._ws = await self._session.ws_connect(url, headers=headers)
        self._recv_task = asyncio.create_task(self._recv_loop())

    async def send_audio(self, audio_chunk: bytes) -> None:
        if self._ws is None:
            raise RuntimeError("Not connected. Call connect() first.")

        if not self._header_sent:
            header = _create_streaming_wav_header(self.sample_rate, NUM_CHANNELS)
            await self._ws.send_bytes(header)
            self._header_sent = True

        await self._ws.send_bytes(audio_chunk)

    async def receive_transcripts(self) -> AsyncIterator[Transcript]:
        if self._ws is None:
            raise RuntimeError("Not connected. Call connect() first.")
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    parsed = self._parse_message(msg.data)
                    if parsed is not None:
                        await self._queue.put(parsed)
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                ):
                    break
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    break
        finally:
            await self._queue.put(None)

    @staticmethod
    def _parse_message(raw: str) -> Transcript | None:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None

        transcript = data.get("transcript", "")
        if not transcript:
            return None

        return Transcript(
            text=transcript,
            is_final=bool(data.get("is_final", False)),
            confidence=float(data.get("confidence", 0.0)),
        )

    async def close(self) -> None:
        if self._recv_task is not None:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except (asyncio.CancelledError, Exception):
                pass
            self._recv_task = None

        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        if self._owns_session and self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None
