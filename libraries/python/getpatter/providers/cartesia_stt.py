"""
Cartesia STT (ink-whisper) adapter for the Patter SDK pipeline mode.

Implements the STTProvider ABC using Cartesia's streaming WebSocket API.
Pure-aiohttp transport — does NOT depend on the vendor SDK.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import AsyncIterator, Literal
from urllib.parse import urlencode

import aiohttp

from getpatter.providers.base import STTProvider, Transcript

logger = logging.getLogger("getpatter")

# Cartesia REST/WS base and protocol constants (port of cartesia/constants.py).
DEFAULT_BASE_URL = "https://api.cartesia.ai"
API_VERSION = "2025-04-16"
USER_AGENT = "Patter/1.0 (provider=Cartesia)"
KEEPALIVE_INTERVAL_SECONDS = 30.0


class CartesiaSTTModel(StrEnum):
    """Known Cartesia STT models."""

    INK_WHISPER = "ink-whisper"


class CartesiaSTTEncoding(StrEnum):
    """Audio encodings accepted by Cartesia's STT websocket endpoint."""

    PCM_S16LE = "pcm_s16le"


class CartesiaSTTSampleRate(IntEnum):
    """Common PCM sample rates accepted by Cartesia STT."""

    HZ_8000 = 8000
    HZ_16000 = 16000
    HZ_24000 = 24000
    HZ_44100 = 44100
    HZ_48000 = 48000


class CartesiaSTTServerEvent(StrEnum):
    """Cartesia STT server event ``type`` values."""

    TRANSCRIPT = "transcript"
    FLUSH_DONE = "flush_done"
    DONE = "done"
    ERROR = "error"


class CartesiaSTTClientFrame(StrEnum):
    """Cartesia STT client-side text frames."""

    FINALIZE = "finalize"


# Backward-compatible Literal aliases — preserved verbatim.
CartesiaEncoding = Literal["pcm_s16le"]
CartesiaModel = Literal["ink-whisper"]


@dataclass
class CartesiaSTTOptions:
    """Configuration for :class:`CartesiaSTT`.

    See https://docs.cartesia.ai/2025-04-16/api-reference/stt/stt
    """

    model: Union[CartesiaSTTModel, str] = CartesiaSTTModel.INK_WHISPER
    language: str = "en"
    encoding: Union[CartesiaSTTEncoding, str] = CartesiaSTTEncoding.PCM_S16LE
    sample_rate: Union[CartesiaSTTSampleRate, int] = CartesiaSTTSampleRate.HZ_16000


class CartesiaSTT(STTProvider):
    """Cartesia STT adapter.

    Streams PCM audio over WebSocket to Cartesia's STT endpoint and yields
    :class:`~getpatter.providers.base.Transcript` objects. Cartesia emits
    interim + final transcripts distinguished by the ``is_final`` boolean
    on the ``transcript`` event.

    Args:
        api_key: Cartesia API key. Required.
        language: BCP-47 language code (``"en"``, ``"es"``, ``"fr"``, ...).
        model: Cartesia STT model; currently only ``"ink-whisper"``.
        encoding: Audio encoding. Only ``"pcm_s16le"`` is supported.
        sample_rate: PCM sample rate in Hz. Cartesia accepts common rates
            (8000, 16000, 24000, 44100, 48000).
        base_url: Override base URL (HTTP or WS). Defaults to Cartesia prod.
        options: Full :class:`CartesiaSTTOptions`; overrides the individual
            kwargs when both are provided.
    """

    def __init__(
        self,
        api_key: str,
        *,
        language: str = "en",
        model: Union[CartesiaSTTModel, str] = CartesiaSTTModel.INK_WHISPER,
        encoding: Union[CartesiaSTTEncoding, str] = CartesiaSTTEncoding.PCM_S16LE,
        sample_rate: Union[CartesiaSTTSampleRate, int] = CartesiaSTTSampleRate.HZ_16000,
        base_url: str = DEFAULT_BASE_URL,
        options: CartesiaSTTOptions | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("CartesiaSTT requires a non-empty api_key")

        if options is None:
            options = CartesiaSTTOptions(
                model=model,
                language=language,
                encoding=encoding,
                sample_rate=sample_rate,
            )

        self._api_key = api_key
        self._base_url = base_url
        self._opts = options

        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._recv_task: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._transcript_queue: asyncio.Queue[Transcript] = asyncio.Queue()
        self._running = False
        self.request_id: str | None = None

    def __repr__(self) -> str:
        return (
            f"CartesiaSTT(model={self._opts.model!r}, "
            f"language={self._opts.language!r}, sample_rate={self._opts.sample_rate})"
        )

    def _build_ws_url(self) -> str:
        """Construct the WebSocket URL with query parameters.

        Cartesia accepts auth via the ``api_key`` query param for the STT
        streaming endpoint.
        """
        base = self._base_url
        if base.startswith("http://"):
            base = "ws://" + base[len("http://") :]
        elif base.startswith("https://"):
            base = "wss://" + base[len("https://") :]
        elif not (base.startswith("ws://") or base.startswith("wss://")):
            base = "wss://" + base

        params = {
            "model": self._opts.model,
            "sample_rate": str(self._opts.sample_rate),
            "encoding": self._opts.encoding,
            "cartesia_version": API_VERSION,
            "api_key": self._api_key,
        }
        if self._opts.language:
            params["language"] = self._opts.language
        return f"{base}/stt/websocket?{urlencode(params)}"

    async def connect(self) -> None:
        """Open the WebSocket and start recv + keepalive tasks."""
        if self._session is None:
            self._session = aiohttp.ClientSession()

        ws_url = self._build_ws_url()
        headers = {"User-Agent": USER_AGENT}
        self._ws = await self._session.ws_connect(ws_url, headers=headers)
        self._running = True
        self._recv_task = asyncio.create_task(self._recv_loop())
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def send_audio(self, audio_chunk: bytes) -> None:
        """Forward a PCM s16le audio chunk to Cartesia."""
        if self._ws is None or self._ws.closed:
            raise RuntimeError("Not connected. Call connect() first.")
        await self._ws.send_bytes(audio_chunk)

    async def receive_transcripts(self) -> AsyncIterator[Transcript]:
        """Async generator yielding :class:`Transcript` events as they arrive."""
        while self._running or not self._transcript_queue.empty():
            try:
                transcript = await asyncio.wait_for(
                    self._transcript_queue.get(), timeout=0.1
                )
            except asyncio.TimeoutError:
                continue
            yield transcript

    async def _keepalive_loop(self) -> None:
        """Send WebSocket pings every 30 s to keep the session alive."""
        try:
            while self._running and self._ws is not None and not self._ws.closed:
                await asyncio.sleep(KEEPALIVE_INTERVAL_SECONDS)
                try:
                    await self._ws.ping()
                except Exception:  # noqa: BLE001
                    break
        except asyncio.CancelledError:
            return

    async def _recv_loop(self) -> None:
        """Read JSON frames from Cartesia and enqueue Transcripts."""
        assert self._ws is not None  # noqa: S101 — guaranteed by caller
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        self._handle_event(json.loads(msg.data))
                    except Exception:  # noqa: BLE001
                        logger.exception("CartesiaSTT failed to process message")
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.ERROR,
                ):
                    break
        finally:
            self._running = False

    def _handle_event(self, data: dict) -> None:
        """Parse a single Cartesia event and enqueue a Transcript if relevant.

        Protocol message types:
        - ``transcript``: interim (is_final=False) or final (is_final=True)
        - ``flush_done``: acknowledgement of a ``finalize`` request
        - ``done``: session is closing cleanly
        - ``error``: server-side error
        """
        message_type = data.get("type")
        if message_type == CartesiaSTTServerEvent.TRANSCRIPT:
            text = (data.get("text") or "").strip()
            is_final = bool(data.get("is_final", False))
            if not text and not is_final:
                return
            request_id = data.get("request_id")
            if request_id:
                self.request_id = request_id
            confidence = float(data.get("probability", 1.0) or 0.0)
            if text:
                self._transcript_queue.put_nowait(
                    Transcript(text=text, is_final=is_final, confidence=confidence)
                )
            return

        if message_type == CartesiaSTTServerEvent.ERROR:
            logger.error("Cartesia STT error: %s", data.get("message", "unknown"))
            return

        if message_type in (
            CartesiaSTTServerEvent.FLUSH_DONE,
            CartesiaSTTServerEvent.DONE,
        ):
            if message_type == CartesiaSTTServerEvent.DONE:
                self._running = False
            return

    async def close(self) -> None:
        """Send ``finalize``, close the WS, cancel tasks."""
        self._running = False
        if self._ws is not None and not self._ws.closed:
            try:
                await self._ws.send_str(CartesiaSTTClientFrame.FINALIZE.value)
            except Exception:  # noqa: BLE001
                pass
            await self._ws.close()
        self._ws = None

        for task in (self._recv_task, self._keepalive_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._recv_task = None
        self._keepalive_task = None

        if self._session is not None:
            await self._session.close()
            self._session = None
