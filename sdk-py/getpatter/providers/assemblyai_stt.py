"""
AssemblyAI Universal Streaming STT adapter for the Patter SDK pipeline mode.

Implements the STTProvider ABC using AssemblyAI's v3 streaming WebSocket API.
Pure-aiohttp transport — does NOT depend on the vendor SDK.

Algorithm adapted from LiveKit Agents (Apache 2.0):
https://github.com/livekit/agents
Source: livekit-plugins/livekit-plugins-assemblyai/livekit/plugins/assemblyai/stt.py
Upstream ref SHA: 78a66bcf79c5cea82989401c408f1dff4b961a5b
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import AsyncIterator, Literal
from urllib.parse import urlencode

import aiohttp

from getpatter.providers.base import STTProvider, Transcript

logger = logging.getLogger("patter")

DEFAULT_BASE_URL = "wss://streaming.assemblyai.com"
DEFAULT_MIN_TURN_SILENCE_MS = 100

Encoding = Literal["pcm_s16le", "pcm_mulaw"]
SpeechModel = Literal[
    "universal-streaming-english",
    "universal-streaming-multilingual",
    "u3-rt-pro",
]


@dataclass
class AssemblyAISTTOptions:
    """Configuration options for AssemblyAISTT.

    Attributes map 1:1 to AssemblyAI's v3 /ws query parameters.
    See https://www.assemblyai.com/docs/universal-streaming
    """

    sample_rate: int = 16000
    encoding: Encoding = "pcm_s16le"
    model: SpeechModel = "universal-streaming-english"
    language_detection: bool | None = None
    end_of_turn_confidence_threshold: float | None = None
    min_turn_silence: int | None = DEFAULT_MIN_TURN_SILENCE_MS
    max_turn_silence: int | None = None
    format_turns: bool | None = None
    keyterms_prompt: list[str] | None = None
    prompt: str | None = None
    vad_threshold: float | None = None
    speaker_labels: bool | None = None
    max_speakers: int | None = None
    domain: str | None = None


class AssemblyAISTT(STTProvider):
    """AssemblyAI Universal Streaming STT adapter.

    Wraps AssemblyAI's v3 WebSocket streaming API behind Patter's
    :class:`~getpatter.providers.base.STTProvider` interface. Audio is forwarded
    as raw ``send_bytes`` frames; the server emits JSON frames with ``type``
    ``"Begin"``, ``"Turn"``, ``"SpeechStarted"`` and ``"Termination"``.

    Only the ``Turn`` messages are surfaced as :class:`Transcript` objects —
    interim transcripts (``words`` array only, no ``end_of_turn``) are yielded
    with ``is_final=False`` and the cumulative text; a final transcript is
    yielded when ``end_of_turn=True`` with the full utterance.

    Args:
        api_key: AssemblyAI API key. Required.
        language: Hint language for STTProvider symmetry. Note that AssemblyAI
            does not take a free-form language code; use ``model`` to select
            the appropriate English/multilingual model.
        model: One of ``"universal-streaming-english"``,
            ``"universal-streaming-multilingual"`` or ``"u3-rt-pro"``.
        encoding: ``"pcm_s16le"`` (default, 16-bit PCM) or ``"pcm_mulaw"``
            (G.711 mu-law, 8 kHz telephony).
        sample_rate: PCM sample rate in Hz. 16000 for high-quality input,
            8000 for telephony (paired with ``encoding="pcm_mulaw"``).
        base_url: Override for the streaming endpoint (e.g. EU: ``wss://streaming.eu.assemblyai.com``).
        options: Fine-grained :class:`AssemblyAISTTOptions`. Overrides individual
            kwargs above when both are provided.
    """

    def __init__(
        self,
        api_key: str,
        *,
        language: str = "en",
        model: SpeechModel = "universal-streaming-english",
        encoding: Encoding = "pcm_s16le",
        sample_rate: int = 16000,
        base_url: str = DEFAULT_BASE_URL,
        options: AssemblyAISTTOptions | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("AssemblyAISTT requires a non-empty api_key")

        if options is None:
            options = AssemblyAISTTOptions(
                sample_rate=sample_rate,
                encoding=encoding,
                model=model,
            )

        self._api_key = api_key
        self._language = language
        self._base_url = base_url
        self._opts = options

        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._recv_task: asyncio.Task[None] | None = None
        self._transcript_queue: asyncio.Queue[Transcript] = asyncio.Queue()
        self._running = False
        self.session_id: str | None = None
        self.expires_at: int | None = None

    def __repr__(self) -> str:
        return (
            f"AssemblyAISTT(model={self._opts.model!r}, "
            f"encoding={self._opts.encoding!r}, sample_rate={self._opts.sample_rate})"
        )

    @classmethod
    def for_twilio(
        cls,
        api_key: str,
        *,
        language: str = "en",
        model: SpeechModel = "universal-streaming-english",
    ) -> "AssemblyAISTT":
        """Create an AssemblyAI adapter configured for Twilio mulaw 8 kHz."""
        return cls(
            api_key=api_key,
            language=language,
            model=model,
            encoding="pcm_mulaw",
            sample_rate=8000,
        )

    def _build_url(self) -> str:
        opts = self._opts

        # u3-rt-pro defaults: min=100, max=min (so both 100 unless overridden)
        if opts.model == "u3-rt-pro":
            min_silence = opts.min_turn_silence if opts.min_turn_silence is not None else 100
            max_silence = opts.max_turn_silence if opts.max_turn_silence is not None else min_silence
        else:
            min_silence = opts.min_turn_silence
            max_silence = opts.max_turn_silence

        # Default language_detection: True for multilingual & u3-rt-pro, else False.
        if opts.language_detection is None:
            language_detection = (
                "multilingual" in opts.model or opts.model == "u3-rt-pro"
            )
        else:
            language_detection = opts.language_detection

        raw_config: dict[str, object | None] = {
            "sample_rate": opts.sample_rate,
            "encoding": opts.encoding,
            "speech_model": opts.model,
            "format_turns": opts.format_turns,
            "end_of_turn_confidence_threshold": opts.end_of_turn_confidence_threshold,
            "min_turn_silence": min_silence,
            "max_turn_silence": max_silence,
            "keyterms_prompt": (
                json.dumps(opts.keyterms_prompt)
                if opts.keyterms_prompt is not None
                else None
            ),
            "language_detection": language_detection,
            "prompt": opts.prompt,
            "vad_threshold": opts.vad_threshold,
            "speaker_labels": opts.speaker_labels,
            "max_speakers": opts.max_speakers,
            "domain": opts.domain,
        }

        filtered: dict[str, str] = {}
        for key, val in raw_config.items():
            if val is None:
                continue
            if isinstance(val, bool):
                filtered[key] = "true" if val else "false"
            else:
                filtered[key] = str(val)

        return f"{self._base_url}/v3/ws?{urlencode(filtered)}"

    async def connect(self) -> None:
        """Open the WebSocket to AssemblyAI and start the recv loop."""
        if self._session is None:
            self._session = aiohttp.ClientSession()

        url = self._build_url()
        headers = {
            "Authorization": self._api_key,
            "Content-Type": "application/json",
            "User-Agent": "Patter/1.0 (integration=LiveKit-port)",
        }
        self._ws = await self._session.ws_connect(url, headers=headers)
        self._running = True
        self._recv_task = asyncio.create_task(self._recv_loop())

    async def send_audio(self, audio_chunk: bytes) -> None:
        """Forward a PCM/mulaw audio chunk to AssemblyAI."""
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

    async def _recv_loop(self) -> None:
        """Read JSON frames from AssemblyAI and enqueue Transcripts."""
        assert self._ws is not None  # noqa: S101 — guaranteed by caller
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        self._handle_event(json.loads(msg.data))
                    except Exception:  # noqa: BLE001
                        logger.exception("AssemblyAISTT failed to process message")
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
        """Parse a single AssemblyAI event and enqueue a Transcript if relevant.

        Message types follow the Universal Streaming v3 protocol:
        - ``Begin``: session started
        - ``Turn``: interim or final transcript
        - ``SpeechStarted``: VAD start marker (not surfaced)
        - ``Termination``: session closed
        """
        message_type = data.get("type")

        if message_type == "Begin":
            self.session_id = data.get("id")
            self.expires_at = data.get("expires_at")
            return

        if message_type == "Termination":
            self._running = False
            return

        if message_type != "Turn":
            return

        end_of_turn = bool(data.get("end_of_turn", False))
        turn_is_formatted = bool(data.get("turn_is_formatted", False))
        transcript_text: str = data.get("transcript", "") or ""
        words = data.get("words", []) or []

        if end_of_turn:
            # If format_turns was requested, wait until the formatted version arrives.
            want_formatted = bool(self._opts.format_turns)
            if want_formatted and not turn_is_formatted:
                return

            text = transcript_text.strip()
            if not text:
                return
            confidence = _average_confidence(words)
            self._transcript_queue.put_nowait(
                Transcript(text=text, is_final=True, confidence=confidence)
            )
            return

        # Interim transcript: assemble from cumulative words list.
        if not words:
            return
        interim_text = " ".join(word.get("text", "") for word in words).strip()
        if not interim_text:
            return
        confidence = _average_confidence(words)
        self._transcript_queue.put_nowait(
            Transcript(text=interim_text, is_final=False, confidence=confidence)
        )

    async def close(self) -> None:
        """Send the termination frame and close resources."""
        self._running = False
        if self._ws is not None and not self._ws.closed:
            try:
                await self._ws.send_str(json.dumps({"type": "Terminate"}))
            except Exception:  # noqa: BLE001
                pass
            await self._ws.close()
        self._ws = None

        if self._recv_task is not None:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._recv_task = None

        if self._session is not None:
            await self._session.close()
            self._session = None


def _average_confidence(words: list[dict]) -> float:
    """Average word-level confidence; returns 0.0 for empty input."""
    if not words:
        return 0.0
    total = sum(float(w.get("confidence", 0.0) or 0.0) for w in words)
    return total / len(words)
