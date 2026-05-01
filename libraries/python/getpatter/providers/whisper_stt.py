"""OpenAI Whisper STT adapter for the Patter SDK pipeline mode."""

from __future__ import annotations

import asyncio
import io
import logging
import wave
from enum import StrEnum
from typing import AsyncIterator, Literal

import httpx

from getpatter.providers.base import STTProvider, Transcript

logger = logging.getLogger("getpatter")


class WhisperModel(StrEnum):
    """Models accepted by ``POST /v1/audio/transcriptions``.

    ``gpt-4o-transcribe`` and ``gpt-4o-mini-transcribe`` were added alongside
    the realtime-mini GA and share the same endpoint.
    """

    WHISPER_1 = "whisper-1"
    GPT_4O_TRANSCRIBE = "gpt-4o-transcribe"
    GPT_4O_MINI_TRANSCRIBE = "gpt-4o-mini-transcribe"


class WhisperResponseFormat(StrEnum):
    """Response formats accepted by ``POST /v1/audio/transcriptions``."""

    JSON = "json"
    VERBOSE_JSON = "verbose_json"


class _Transcript(Transcript):  # type: ignore[misc]
    """Backward-compat shim for code that imported the private class.

    ``Transcript`` from ``providers.base`` is a frozen-ish dataclass; this
    subclass lets callers do ``_Transcript(text="x")`` (positional text,
    defaults for the rest) the way the old ad-hoc class allowed.
    """

    def __init__(
        self, text: str, is_final: bool = True, confidence: float = 1.0
    ) -> None:
        super().__init__(text=text, is_final=is_final, confidence=confidence)


OPENAI_TRANSCRIPTION_URL = "https://api.openai.com/v1/audio/transcriptions"
# ~1 second of 16 kHz 16-bit mono audio
BUFFER_SIZE_BYTES = 16000 * 2

# Models accepted by ``POST /v1/audio/transcriptions``. ``gpt-4o-transcribe``
# and ``gpt-4o-mini-transcribe`` were added alongside the realtime-mini GA
# and share the same endpoint.
_ALLOWED_MODELS = {m.value for m in WhisperModel}


class WhisperSTT(STTProvider):
    """Whisper (OpenAI) STT adapter — buffers PCM audio and transcribes in chunks.

    Compatible with the DeepgramSTT interface so it can be swapped in pipeline
    mode without changes to the calling code.

    Args:
        api_key: OpenAI API key.
        language: BCP-47 language code (e.g. ``"en"``).
        model: Whisper model to use. One of ``"whisper-1"``,
            ``"gpt-4o-transcribe"``, ``"gpt-4o-mini-transcribe"``.
        response_format: ``"json"`` (default) or ``"verbose_json"`` to
            surface per-segment timestamps / confidence.
    """

    def __init__(
        self,
        api_key: str,
        language: str = "en",
        model: Union[WhisperModel, str] = WhisperModel.WHISPER_1,
        response_format: Union[
            WhisperResponseFormat, Literal["json", "verbose_json"]
        ] = WhisperResponseFormat.JSON,
    ) -> None:
        if model not in _ALLOWED_MODELS:
            raise ValueError(
                f"WhisperSTT: unsupported model {model!r}. "
                f"Expected one of {sorted(_ALLOWED_MODELS)}."
            )
        self.api_key = api_key
        self.language = language
        self.model = model
        self.response_format = response_format
        self._buffer = bytearray()
        self._transcript_queue: asyncio.Queue[Transcript] = asyncio.Queue()
        self._running = False
        self._pending: set[asyncio.Task] = set()
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10.0,
        )

    @classmethod
    def for_twilio(
        cls,
        api_key: str,
        language: str = "en",
        model: Union[WhisperModel, str] = WhisperModel.WHISPER_1,
    ) -> "WhisperSTT":
        """Factory mirroring the TS ``forTwilio`` helper.

        Twilio delivers mulaw 8 kHz that the upstream transcoder converts
        to PCM16 16 kHz before reaching this adapter, so no extra config
        is needed today — the factory exists only for API parity.
        """
        return cls(api_key=api_key, language=language, model=model)

    async def connect(self) -> None:
        """Initialise the adapter (no persistent connection needed for Whisper)."""
        self._running = True
        self._buffer = bytearray()

    async def send_audio(self, audio_chunk: bytes) -> None:
        """Buffer incoming PCM audio and transcribe when the buffer is full."""
        self._buffer.extend(audio_chunk)
        if len(self._buffer) >= BUFFER_SIZE_BYTES:
            buf = bytes(self._buffer)
            self._buffer.clear()
            task = asyncio.create_task(self._transcribe_and_enqueue(buf))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)

    async def _transcribe_and_enqueue(self, pcm_data: bytes) -> None:
        transcript = await self._transcribe_buffer(pcm_data)
        if transcript:
            await self._transcript_queue.put(transcript)

    async def _transcribe_buffer(self, pcm_data: bytes) -> Transcript | None:
        """Send a PCM buffer to the Whisper API and return the transcript."""
        wav_buf = io.BytesIO()
        with wave.open(wav_buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(pcm_data)
        wav_buf.seek(0)
        try:
            data = {
                "model": self.model,
                "language": self.language,
                "response_format": self.response_format,
            }
            resp = await self._client.post(
                OPENAI_TRANSCRIPTION_URL,
                files={"file": ("audio.wav", wav_buf, "audio/wav")},
                data=data,
            )
            resp.raise_for_status()
            payload = resp.json()
            text = (payload.get("text") or "").strip()
            if not text:
                return None
            confidence = _extract_confidence(payload)
            return Transcript(text=text, is_final=True, confidence=confidence)
        except Exception as exc:
            logger.exception("WhisperSTT transcription error: %s", exc)
            return None

    async def receive_transcripts(self) -> AsyncIterator[Transcript]:
        """Async generator that yields transcripts as they arrive."""
        while self._running:
            try:
                yield await asyncio.wait_for(self._transcript_queue.get(), timeout=0.1)
            except asyncio.TimeoutError:
                continue

    async def close(self) -> None:
        """Flush remaining buffer and close the HTTP client.

        Always flushes whatever audio remains in the buffer (when non-empty)
        so the trailing 0–250 ms before end-of-utterance are not silently
        dropped. Previously the buffer was only transcribed when it had
        accumulated more than ~25% of ``BUFFER_SIZE_BYTES``, which discarded
        short tail-end utterances entirely.
        """
        self._running = False
        if len(self._buffer) > 0:
            transcript = await self._transcribe_buffer(bytes(self._buffer))
            if transcript:
                await self._transcript_queue.put(transcript)
        self._buffer.clear()
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)
        await self._client.aclose()


def _extract_confidence(payload: dict) -> float:
    """Derive a confidence score from the Whisper verbose_json segments.

    OpenAI returns per-segment ``avg_logprob`` in verbose_json. We convert
    via ``exp(avg_logprob)`` averaged across segments and clamp to [0, 1].
    When the payload doesn't carry segment data we fall back to 1.0 so
    existing consumers keep their current behaviour.
    """
    segments = payload.get("segments")
    if not segments:
        return 1.0
    import math

    scores: list[float] = []
    for seg in segments:
        logp = seg.get("avg_logprob")
        if isinstance(logp, (int, float)):
            scores.append(max(0.0, min(1.0, math.exp(float(logp)))))
    if not scores:
        return 1.0
    return sum(scores) / len(scores)
