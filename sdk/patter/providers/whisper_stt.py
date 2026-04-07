"""OpenAI Whisper STT adapter for the Patter SDK pipeline mode."""

from __future__ import annotations

import asyncio
import io
import logging
import wave
from typing import AsyncIterator

import httpx

logger = logging.getLogger("patter")

OPENAI_TRANSCRIPTION_URL = "https://api.openai.com/v1/audio/transcriptions"
# ~1 second of 16 kHz 16-bit mono audio
BUFFER_SIZE_BYTES = 16000 * 2


class _Transcript:
    """Lightweight transcript result compatible with DeepgramSTT output."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.is_final = True
        self.confidence = 1.0


class WhisperSTT:
    """Whisper (OpenAI) STT adapter — buffers PCM audio and transcribes in chunks.

    Compatible with the DeepgramSTT interface so it can be swapped in pipeline
    mode without changes to the calling code.

    Args:
        api_key: OpenAI API key.
        language: BCP-47 language code (e.g. ``"en"``).
        model: Whisper model to use (default ``"whisper-1"``).
    """

    def __init__(
        self,
        api_key: str,
        language: str = "en",
        model: str = "whisper-1",
    ) -> None:
        self.api_key = api_key
        self.language = language
        self.model = model
        self._buffer = bytearray()
        self._transcript_queue: asyncio.Queue[_Transcript] = asyncio.Queue()
        self._running = False
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10.0,
        )

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
            transcript = await self._transcribe_buffer(buf)
            if transcript:
                await self._transcript_queue.put(transcript)

    async def _transcribe_buffer(self, pcm_data: bytes) -> _Transcript | None:
        """Send a PCM buffer to the Whisper API and return the transcript."""
        wav_buf = io.BytesIO()
        with wave.open(wav_buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(pcm_data)
        wav_buf.seek(0)
        try:
            resp = await self._client.post(
                OPENAI_TRANSCRIPTION_URL,
                files={"file": ("audio.wav", wav_buf, "audio/wav")},
                data={"model": self.model, "language": self.language},
            )
            resp.raise_for_status()
            text = resp.json().get("text", "").strip()
            return _Transcript(text=text) if text else None
        except Exception as exc:
            logger.exception("WhisperSTT transcription error: %s", exc)
            return None

    async def receive_transcripts(self) -> AsyncIterator[_Transcript]:
        """Async generator that yields transcripts as they arrive."""
        while self._running:
            try:
                yield await asyncio.wait_for(self._transcript_queue.get(), timeout=0.1)
            except asyncio.TimeoutError:
                continue

    async def close(self) -> None:
        """Flush remaining buffer and close the HTTP client."""
        self._running = False
        if len(self._buffer) > BUFFER_SIZE_BYTES // 4:
            transcript = await self._transcribe_buffer(bytes(self._buffer))
            if transcript:
                await self._transcript_queue.put(transcript)
        self._buffer.clear()
        await self._client.aclose()
