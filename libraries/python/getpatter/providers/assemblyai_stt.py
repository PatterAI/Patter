"""
AssemblyAI Universal Streaming STT adapter for the Patter SDK pipeline mode.

Implements the STTProvider ABC using AssemblyAI's v3 streaming WebSocket API.
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

DEFAULT_BASE_URL = "wss://streaming.assemblyai.com"
DEFAULT_MIN_TURN_SILENCE_MS = 400
TERMINATION_WAIT_TIMEOUT_S = 0.5
MIN_CHUNK_DURATION_MS = 50
MAX_CHUNK_DURATION_MS = 1000
# Target send size in milliseconds for the coalescing buffer. Sits one
# Twilio frame (20 ms) above AssemblyAI's 50 ms floor so jitter never
# dips below the protocol minimum (server emits error 3007 below 50 ms).
DEFAULT_TARGET_CHUNK_MS = 60
RECONNECT_ERROR_CODES = {3005, 3008}


class AssemblyAIEncoding(StrEnum):
    """Audio encodings accepted by AssemblyAI's v3 streaming endpoint."""

    PCM_S16LE = "pcm_s16le"
    PCM_MULAW = "pcm_mulaw"


class AssemblyAIModel(StrEnum):
    """Known AssemblyAI Universal Streaming speech models."""

    UNIVERSAL_STREAMING_ENGLISH = "universal-streaming-english"
    UNIVERSAL_STREAMING_MULTILINGUAL = "universal-streaming-multilingual"
    U3_RT_PRO = "u3-rt-pro"
    WHISPER_RT = "whisper-rt"


class AssemblyAIDomain(StrEnum):
    """Valid ``domain`` values for AssemblyAI's v3 streaming endpoint."""

    GENERAL = "general"
    MEDICAL_V1 = "medical-v1"


class AssemblyAISampleRate(IntEnum):
    """Common PCM sample rates for AssemblyAI streaming input."""

    HZ_8000 = 8000
    HZ_16000 = 16000


class AssemblyAIEventType(StrEnum):
    """AssemblyAI v3 streaming server event types."""

    BEGIN = "Begin"
    TURN = "Turn"
    SPEECH_STARTED = "SpeechStarted"
    TERMINATION = "Termination"


class AssemblyAIClientFrame(StrEnum):
    """AssemblyAI v3 streaming client-side message types."""

    UPDATE_CONFIGURATION = "UpdateConfiguration"
    FORCE_ENDPOINT = "ForceEndpoint"
    TERMINATE = "Terminate"


VALID_DOMAINS = {AssemblyAIDomain.GENERAL.value, AssemblyAIDomain.MEDICAL_V1.value}

# Backward-compatible Literal aliases — preserved verbatim for callers that
# already type their kwargs against these names. ``StrEnum`` members compare
# equal to their string values, so passing either an enum member or a plain
# string works identically.
Encoding = Literal["pcm_s16le", "pcm_mulaw"]
SpeechModel = Literal[
    "universal-streaming-english",
    "universal-streaming-multilingual",
    "u3-rt-pro",
    "whisper-rt",
]


@dataclass
class AssemblyAISTTOptions:
    """Configuration options for AssemblyAISTT.

    Attributes map 1:1 to AssemblyAI's v3 /ws query parameters.
    See https://www.assemblyai.com/docs/universal-streaming
    """

    sample_rate: Union[AssemblyAISampleRate, int] = AssemblyAISampleRate.HZ_16000
    encoding: Union[AssemblyAIEncoding, str] = AssemblyAIEncoding.PCM_S16LE
    model: Union[AssemblyAIModel, str] = AssemblyAIModel.UNIVERSAL_STREAMING_ENGLISH
    language_detection: bool | None = None
    end_of_turn_confidence_threshold: float | None = None
    min_turn_silence: int | None = DEFAULT_MIN_TURN_SILENCE_MS
    max_turn_silence: int | None = None
    format_turns: bool | None = None
    keyterms_prompt: list[str] | None = None
    prompt: str | None = None
    # Accepted for backward compatibility but NOT sent — not a valid v3 param.
    vad_threshold: float | None = None
    speaker_labels: bool | None = None
    max_speakers: int | None = None
    domain: Union[AssemblyAIDomain, str, None] = None


class AssemblyAISTT(STTProvider):
    """AssemblyAI Universal Streaming STT adapter.

    Wraps AssemblyAI's v3 WebSocket streaming API behind Patter's
    :class:`~getpatter.providers.base.STTProvider` interface. Audio is forwarded
    as raw ``send_bytes`` frames; the server emits JSON frames with ``type``
    ``"Begin"``, ``"Turn"``, ``"SpeechStarted"`` and ``"Termination"``.

    ``Turn`` messages are surfaced as :class:`Transcript` objects. Interim
    transcripts are yielded with ``is_final=False``; finals with ``is_final=True``.
    ``SpeechStarted`` is surfaced as a zero-text interim transcript for
    downstream barge-in logic.

    Args:
        api_key: AssemblyAI API key. Required.
        language: Hint language for STTProvider symmetry. Currently ignored —
            AssemblyAI selects language behaviour via ``model``. A warning is
            logged when set to any non-default value.
        model: One of ``"universal-streaming-english"``,
            ``"universal-streaming-multilingual"``, ``"u3-rt-pro"``,
            or ``"whisper-rt"``.
        encoding: ``"pcm_s16le"`` (default, 16-bit PCM) or ``"pcm_mulaw"``
            (G.711 mu-law, 8 kHz telephony).
        sample_rate: PCM sample rate in Hz.
        base_url: Override for the streaming endpoint.
        use_query_token: When ``True``, authenticate via ``?token=<api_key>`` in
            the URL instead of the ``Authorization`` header. Default ``False``.
        options: Fine-grained :class:`AssemblyAISTTOptions`.
    """

    def __init__(
        self,
        api_key: str,
        *,
        language: str = "en",
        model: Union[
            AssemblyAIModel, str
        ] = AssemblyAIModel.UNIVERSAL_STREAMING_ENGLISH,
        encoding: Union[AssemblyAIEncoding, str] = AssemblyAIEncoding.PCM_S16LE,
        sample_rate: Union[AssemblyAISampleRate, int] = AssemblyAISampleRate.HZ_16000,
        base_url: str = DEFAULT_BASE_URL,
        use_query_token: bool = False,
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

        if options.domain is not None and options.domain not in VALID_DOMAINS:
            hint = ""
            if options.domain == "medical":
                hint = ' — did you mean "medical-v1"?'
            raise ValueError(
                f"AssemblyAISTT: invalid domain {options.domain!r}; "
                f"expected one of {sorted(VALID_DOMAINS)}{hint}"
            )

        if language and language != "en":
            logger.warning(
                "AssemblyAISTT: 'language=%r' is currently ignored; language "
                "selection is driven by the 'model' kwarg.",
                language,
            )

        self._api_key = api_key
        self._language = language
        self._base_url = base_url
        self._use_query_token = use_query_token
        self._opts = options

        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._recv_task: asyncio.Task[None] | None = None
        self._transcript_queue: asyncio.Queue[Transcript] = asyncio.Queue()
        self._running = False
        self._closing_event: asyncio.Event = asyncio.Event()
        self._termination_event: asyncio.Event = asyncio.Event()
        self._reconnect_attempts = 0
        self.session_id: str | None = None
        self.expires_at: int | None = None
        # Coalescing buffer for inbound audio frames. AssemblyAI's v3
        # streaming endpoint requires each ws frame to carry 50–1000 ms
        # of audio (server emits error 3007 below 50 ms — observed in the
        # field as a fully-billed call with zero transcripts). Twilio sends
        # 20 ms frames, so the SDK must batch ~3 frames before forwarding.
        self._audio_buffer: bytearray = bytearray()
        # Target send size in bytes — recomputed lazily once encoding /
        # sample_rate are known.
        self._audio_buffer_target_bytes: int = 0

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
        model: Union[
            AssemblyAIModel, str
        ] = AssemblyAIModel.UNIVERSAL_STREAMING_ENGLISH,
    ) -> "AssemblyAISTT":
        """Create an AssemblyAI adapter configured for Twilio mulaw 8 kHz."""
        return cls(
            api_key=api_key,
            language=language,
            model=model,
            encoding=AssemblyAIEncoding.PCM_MULAW,
            sample_rate=AssemblyAISampleRate.HZ_8000,
        )

    def _build_url(self) -> str:
        opts = self._opts

        # u3-rt-pro defaults: min=100, max=min (so both 100 unless overridden)
        if opts.model == AssemblyAIModel.U3_RT_PRO:
            min_silence = (
                opts.min_turn_silence if opts.min_turn_silence is not None else 100
            )
            max_silence = (
                opts.max_turn_silence
                if opts.max_turn_silence is not None
                else min_silence
            )
        else:
            min_silence = opts.min_turn_silence
            max_silence = opts.max_turn_silence

        # Default language_detection: True for multilingual & u3-rt-pro, else False.
        if opts.language_detection is None:
            language_detection = (
                "multilingual" in str(opts.model)
                or opts.model == AssemblyAIModel.U3_RT_PRO
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
            # vad_threshold intentionally omitted — not a valid v3 parameter.
            "speaker_labels": opts.speaker_labels,
            "max_speakers": opts.max_speakers,
            "domain": opts.domain,
        }

        if self._use_query_token:
            raw_config["token"] = self._api_key

        filtered: dict[str, str] = {}
        for key, val in raw_config.items():
            if val is None:
                continue
            if isinstance(val, bool):
                filtered[key] = "true" if val else "false"
            else:
                filtered[key] = str(val)

        return f"{self._base_url}/v3/ws?{urlencode(filtered)}"

    def _build_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Patter/1.0",
        }
        if not self._use_query_token:
            headers["Authorization"] = self._api_key
        return headers

    async def connect(self) -> None:
        """Open the WebSocket to AssemblyAI and start the recv loop."""
        if self._session is None:
            self._session = aiohttp.ClientSession()

        url = self._build_url()
        headers = self._build_headers()
        self._ws = await self._session.ws_connect(url, headers=headers)
        self._running = True
        self._termination_event.clear()
        self._closing_event.clear()
        self._recv_task = asyncio.create_task(self._recv_loop())

    async def send_audio(self, audio_chunk: bytes) -> None:
        """Forward a PCM/mulaw audio chunk to AssemblyAI, buffered.

        Twilio's media stream emits 20 ms frames (160 bytes mulaw 8 kHz),
        which is below AssemblyAI's 50 ms minimum frame size (server emits
        error 3007 and closes the stream). We coalesce frames into the
        internal :attr:`_audio_buffer` until ~60 ms is accumulated, then
        flush in a single ``send_bytes`` call. Trailing bytes are flushed
        on :meth:`flush_audio` / :meth:`close`.

        Pre-connect / closed-socket calls are silently dropped (mirrors the
        TS adapter): the WS handshake takes 200–500 ms but Twilio starts
        streaming immediately on ``connect`` — losing the first ~10 frames
        is preferable to a hard crash on every call.
        """
        if self._ws is None or self._ws.closed:
            return
        if not audio_chunk:
            return

        if self._audio_buffer_target_bytes == 0:
            self._audio_buffer_target_bytes = self._compute_target_chunk_bytes()

        self._audio_buffer.extend(audio_chunk)
        if len(self._audio_buffer) < self._audio_buffer_target_bytes:
            return

        merged = bytes(self._audio_buffer)
        self._audio_buffer.clear()

        duration_ms = self._estimate_chunk_duration_ms(len(merged))
        if duration_ms is not None and (
            duration_ms < MIN_CHUNK_DURATION_MS or duration_ms > MAX_CHUNK_DURATION_MS
        ):
            logger.warning(
                "AssemblyAISTT: audio chunk duration %.1fms outside 50-1000ms bounds "
                "(may trigger error 3007).",
                duration_ms,
            )

        await self._ws.send_bytes(merged)

    async def flush_audio(self) -> None:
        """Flush any buffered audio to AssemblyAI.

        Called automatically by :meth:`close` so the trailing <60 ms tail
        is not silently dropped at end-of-call. Safe to call repeatedly.
        """
        if self._ws is None or self._ws.closed:
            self._audio_buffer.clear()
            return
        if not self._audio_buffer:
            return
        merged = bytes(self._audio_buffer)
        self._audio_buffer.clear()
        try:
            await self._ws.send_bytes(merged)
        except Exception:  # noqa: BLE001
            # Flush is best-effort during shutdown — never raise.
            logger.debug("AssemblyAISTT: flush_audio failed (socket closing)")

    def _compute_target_chunk_bytes(self) -> int:
        """Bytes corresponding to ``DEFAULT_TARGET_CHUNK_MS`` of audio.

        For mulaw 8 kHz that's 480 bytes (3× Twilio's 20 ms frames); for
        PCM s16le 16 kHz it's 1920 bytes (~60 ms).
        """
        sample_rate = int(self._opts.sample_rate or 0)
        if sample_rate <= 0:
            # Fallback: assume 16 kHz s16le.
            sample_rate = AssemblyAISampleRate.HZ_16000
        if self._opts.encoding == AssemblyAIEncoding.PCM_MULAW:
            return -(-(sample_rate * DEFAULT_TARGET_CHUNK_MS) // 1000)
        # PCM_S16LE: 2 bytes/sample.
        return -(-(sample_rate * DEFAULT_TARGET_CHUNK_MS) // 1000) * 2

    def _estimate_chunk_duration_ms(self, byte_length: int) -> float | None:
        """Estimate chunk duration in ms from byte length and PCM settings."""
        if byte_length <= 0:
            return None
        sample_rate = self._opts.sample_rate or 0
        if sample_rate <= 0:
            return None
        bytes_per_sample = (
            2 if self._opts.encoding == AssemblyAIEncoding.PCM_S16LE else 1
        )
        samples = byte_length / bytes_per_sample
        return (samples / sample_rate) * 1000.0

    async def update_configuration(
        self,
        *,
        keyterms_prompt: list[str] | None = None,
        prompt: str | None = None,
        min_turn_silence: int | None = None,
        max_turn_silence: int | None = None,
    ) -> None:
        """Send an ``UpdateConfiguration`` frame to change settings mid-stream.

        Only non-None fields are included. Used for entity-collection mode
        (e.g. raise ``min_turn_silence`` while a user dictates a phone number).
        """
        if self._ws is None or self._ws.closed:
            raise RuntimeError("Not connected. Call connect() first.")

        payload: dict[str, object] = {
            "type": AssemblyAIClientFrame.UPDATE_CONFIGURATION.value
        }
        if keyterms_prompt is not None:
            payload["keyterms_prompt"] = json.dumps(keyterms_prompt)
        if prompt is not None:
            payload["prompt"] = prompt
        if min_turn_silence is not None:
            payload["min_turn_silence"] = min_turn_silence
        if max_turn_silence is not None:
            payload["max_turn_silence"] = max_turn_silence

        await self._ws.send_str(json.dumps(payload))

    async def force_endpoint(self) -> None:
        """Force the server to finalize the current turn (for barge-in)."""
        if self._ws is None or self._ws.closed:
            raise RuntimeError("Not connected. Call connect() first.")
        await self._ws.send_str(
            json.dumps({"type": AssemblyAIClientFrame.FORCE_ENDPOINT.value})
        )

    async def receive_transcripts(self) -> AsyncIterator[Transcript]:
        """Async generator yielding :class:`Transcript` events as they arrive."""
        while self._running or not self._transcript_queue.empty():
            if self._closing_event.is_set() and self._transcript_queue.empty():
                break
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
            close_code = self._ws.close_code if self._ws is not None else None
            if (
                close_code in RECONNECT_ERROR_CODES
                and not self._closing_event.is_set()
                and self._reconnect_attempts < 1
            ):
                self._reconnect_attempts += 1
                logger.warning(
                    "AssemblyAISTT: close code %s — attempting single reconnect.",
                    close_code,
                )
                try:
                    await self._reconnect()
                except Exception:  # noqa: BLE001
                    logger.exception("AssemblyAISTT reconnect failed")

    async def _reconnect(self) -> None:
        """Re-open the WebSocket and resume the recv loop."""
        if self._session is None:
            self._session = aiohttp.ClientSession()
        url = self._build_url()
        headers = self._build_headers()
        self._ws = await self._session.ws_connect(url, headers=headers)
        self._running = True
        self._termination_event.clear()
        self._recv_task = asyncio.create_task(self._recv_loop())

    def _handle_event(self, data: dict) -> None:
        """Parse a single AssemblyAI event and enqueue a Transcript if relevant.

        Message types follow the Universal Streaming v3 protocol:
        - ``Begin``: session started
        - ``Turn``: interim or final transcript
        - ``SpeechStarted``: VAD start marker — surfaced as an interim event
        - ``Termination``: session closed
        """
        message_type = data.get("type")

        if message_type == AssemblyAIEventType.BEGIN:
            self.session_id = data.get("id")
            self.expires_at = data.get("expires_at")
            return

        if message_type == AssemblyAIEventType.TERMINATION:
            self._running = False
            self._termination_event.set()
            return

        if message_type == AssemblyAIEventType.SPEECH_STARTED:
            # Surface as a zero-text interim transcript so downstream barge-in
            # logic can react. ``event_type`` is a proper field on the shared
            # Transcript dataclass, so consumers can distinguish SpeechStarted
            # from normal interims without dynamic attribute access.
            self._transcript_queue.put_nowait(
                Transcript(
                    text="",
                    is_final=False,
                    confidence=0.0,
                    event_type="SpeechStarted",
                )
            )
            return

        if message_type != AssemblyAIEventType.TURN:
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
        """Send the termination frame and close resources.

        Sends ``Terminate``, waits up to 500 ms for the server ``Termination``
        event, then closes the socket.
        """
        self._closing_event.set()
        self._running = False

        if self._ws is not None and not self._ws.closed:
            # Flush any buffered audio so the trailing <60 ms tail isn't dropped.
            try:
                await self.flush_audio()
            except Exception:  # noqa: BLE001
                pass
            try:
                await self._ws.send_str(
                    json.dumps({"type": AssemblyAIClientFrame.TERMINATE.value})
                )
            except Exception:  # noqa: BLE001
                pass

            try:
                await asyncio.wait_for(
                    self._termination_event.wait(),
                    timeout=TERMINATION_WAIT_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                logger.debug(
                    "AssemblyAISTT: no Termination received within %.0fms; closing.",
                    TERMINATION_WAIT_TIMEOUT_S * 1000,
                )

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
