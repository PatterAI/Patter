"""Telnyx Speech-to-Text provider (WebSocket streaming).

Bridges the Telnyx ``/v2/speech-to-text/transcription`` WebSocket API to
Patter's :class:`~getpatter.providers.base.STTProvider` interface. Streams
PCM audio in and yields :class:`~getpatter.providers.base.Transcript`
events out via :meth:`receive_transcripts`.
"""

from __future__ import annotations

import asyncio
import json
import struct
from enum import IntEnum, StrEnum
from typing import AsyncIterator, Literal

import aiohttp

from getpatter.providers.base import STTProvider, Transcript

TELNYX_STT_WS_URL = "wss://api.telnyx.com/v2/speech-to-text/transcription"


class TelnyxTranscriptionEngine(StrEnum):
    """Backend transcription engines accepted by Telnyx STT."""

    TELNYX = "telnyx"
    GOOGLE = "google"
    DEEPGRAM = "deepgram"
    AZURE = "azure"


class TelnyxSTTSampleRate(IntEnum):
    """Common PCM sample rates accepted by Telnyx STT."""

    HZ_8000 = 8000
    HZ_16000 = 16000
    HZ_24000 = 24000


class TelnyxSTTInputFormat(StrEnum):
    """Input audio formats accepted by Telnyx STT."""

    WAV = "wav"


DEFAULT_SAMPLE_RATE = TelnyxSTTSampleRate.HZ_16000.value
NUM_CHANNELS = 1

# Backward-compatible Literal alias for type hints in older callers.
TranscriptionEngine = Literal["telnyx", "google", "deepgram", "azure"]


def _create_streaming_wav_header(sample_rate: int, num_channels: int) -> bytes:
    """Create a WAV header for streaming with maximum possible size."""
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
        transcription_engine: Union[
            TelnyxTranscriptionEngine, str
        ] = TelnyxTranscriptionEngine.TELNYX,
        sample_rate: Union[TelnyxSTTSampleRate, int] = TelnyxSTTSampleRate.HZ_16000,
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
        """Open the Telnyx Speech-to-Text WebSocket and start the recv loop."""
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._owns_session = True

        params = {
            "transcription_engine": self.transcription_engine,
            "language": self.language,
            "input_format": TelnyxSTTInputFormat.WAV.value,
        }
        query_string = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{self.base_url}?{query_string}"
        headers = {"Authorization": f"Bearer {self.api_key}"}

        self._ws = await self._session.ws_connect(url, headers=headers)
        self._recv_task = asyncio.create_task(self._recv_loop())

    async def send_audio(self, audio_chunk: bytes) -> None:
        """Send a PCM audio chunk; prefixes the streaming WAV header on the first call."""
        if self._ws is None:
            raise RuntimeError("Not connected. Call connect() first.")

        if not self._header_sent:
            header = _create_streaming_wav_header(self.sample_rate, NUM_CHANNELS)
            await self._ws.send_bytes(header)
            self._header_sent = True

        await self._ws.send_bytes(audio_chunk)

    async def receive_transcripts(self) -> AsyncIterator[Transcript]:
        """Yield :class:`Transcript` items as Telnyx returns transcription frames."""
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
        """Cancel the recv task, close the socket, and release the HTTP session."""
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
