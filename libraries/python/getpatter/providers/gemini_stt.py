"""Gemini multimodal Speech-to-Text for the Patter SDK — turn-based adapter.

Beta: ported from the TypeScript adapter (``src/providers/gemini-stt.ts``);
validated against the Google Gen AI SDK surface, not yet exercised against a
live phone call.

Buffers inbound PCM16 and, when the turn ends (:meth:`GeminiSTT.finalize`,
fired by the pipeline at VAD speech-end), sends the whole utterance to
``gemini-2.5-flash`` as audio. The model both transcribes AND reads the
caller's emotional tone from how they sound, emitting::

    [tone: <1-2 words>] <verbatim transcript>

The tone prefix flows downstream to the LLM so the agent can mirror mood (a
flat, tired caller -> a gentler reply), which a words-only transcriber cannot
do. Transcription is turn-based rather than windowed precisely so tone is
judged over the full utterance.

Latency consequence: there are no interim partials, and the transcript for a
turn arrives one model round-trip after speech-end. Prefer a genuinely
streaming STT (Deepgram, Soniox, AssemblyAI, Speechmatics) when turn latency
matters more than tone.

Credential: ``GEMINI_API_KEY`` / ``GOOGLE_API_KEY`` env var, or the ``api_key``
argument.
"""

from __future__ import annotations

import asyncio
import io
import logging
import wave
from typing import Any, AsyncIterator, ClassVar, Optional

from getpatter.providers.base import STTProvider, Transcript
from getpatter.providers.gemini_tts import (
    build_genai_client,
    resolve_gemini_api_key,
)

logger = logging.getLogger("getpatter.providers.gemini_stt")

# Multimodal model that accepts audio input and returns text.
# Source: https://ai.google.dev/gemini-api/docs/models (as of 2026-08-24)
GEMINI_STT_DEFAULT_MODEL = "gemini-2.5-flash"

# The pipeline hands every STT adapter PCM16 mono @ 16 kHz (the carrier bridge
# decodes inbound mu-law before the STT), so that is the default.
GEMINI_STT_DEFAULT_SAMPLE_RATE = 16000

# Low temperature keeps the transcript verbatim; the cap bounds a runaway
# response to roughly one long utterance worth of tokens.
GEMINI_STT_TEMPERATURE = 0.2
GEMINI_STT_MAX_OUTPUT_TOKENS = 300

# Poll interval used while draining the transcript queue (seconds).
_QUEUE_POLL_SECONDS = 0.1

# Bytes per PCM16 mono sample — used to convert buffered bytes into seconds.
_BYTES_PER_SAMPLE = 2

TONE_PROMPT = (
    "You are the transcription stage of a live phone call. Transcribe the "
    "caller verbatim, then judge their emotional tone from HOW they sound — "
    "energy, pace, pitch — not just the words. Output exactly one line and "
    "nothing else: [tone: <1-2 words>] <transcript>. If there is no "
    "intelligible speech, output an empty line."
)


def _pcm_to_wav(pcm_data: bytes, sample_rate: int) -> bytes:
    """Wrap raw PCM16 mono samples in a WAV container (Gemini takes audio/wav)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(_BYTES_PER_SAMPLE)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)
    return buf.getvalue()


def _extract_text(response: Any) -> str:
    """Read the response text, falling back to the candidate parts.

    ``google-genai`` exposes a convenience ``.text`` on the response, but it is
    ``None`` on some shapes (and on hand-built fakes), so the parts are joined
    as a fallback — same two-step read as the TypeScript adapter.
    """
    direct = getattr(response, "text", None)
    if isinstance(direct, str) and direct:
        return direct
    candidates = getattr(response, "candidates", None) or []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None) or []
        joined = "".join(getattr(part, "text", None) or "" for part in parts)
        if joined:
            return joined
    return ""


class GeminiSTT(STTProvider):
    """Turn-based multimodal STT adapter using Gemini for transcript + tone.

    Args:
        api_key: Google Generative Language API key. Falls back to
            ``GEMINI_API_KEY`` then ``GOOGLE_API_KEY``.
        model: Multimodal model id (default ``gemini-2.5-flash``).
        sample_rate: Inbound PCM16 rate from the pipeline (default 16000).
    """

    #: Stable pricing/dashboard key — read by stream-handler/metrics.
    provider_key: ClassVar[str] = "gemini_stt"

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        model: str = GEMINI_STT_DEFAULT_MODEL,
        sample_rate: int = GEMINI_STT_DEFAULT_SAMPLE_RATE,
    ) -> None:
        self.api_key = resolve_gemini_api_key(api_key, "Gemini STT")
        self.model = model
        self.sample_rate = sample_rate
        # Declared so the observability layer can compute audio seconds.
        self.encoding = "linear16"

        self._client: Any = None
        self._buffer = bytearray()
        self._audio_bytes_sent: int = 0
        # Queue holds Transcript items; None is a sentinel that signals the
        # generator to stop after draining all real transcripts.
        self._transcript_queue: asyncio.Queue[Transcript | None] = asyncio.Queue()
        self._running = False
        self._pending: set[asyncio.Task] = set()

    def __repr__(self) -> str:
        # Never leak the API key in repr / logs.
        return f"GeminiSTT(model={self.model!r}, sample_rate={self.sample_rate})"

    def _ensure_client(self) -> Any:
        if self._client is None:
            self._client = build_genai_client(self.api_key, "GeminiSTT")
        return self._client

    def _record_transcript_cost(self) -> None:
        """Emit ``patter.cost.stt_seconds`` for the transcribed audio."""
        try:
            from getpatter.observability.attributes import record_patter_attrs

            seconds = self._audio_bytes_sent / float(
                self.sample_rate * _BYTES_PER_SAMPLE
            )
            record_patter_attrs(
                {
                    "patter.cost.stt_seconds": seconds,
                    "patter.stt.provider": self.provider_key,
                }
            )
            self._audio_bytes_sent = 0
        except Exception:  # pragma: no cover — defense in depth
            logger.debug("_record_transcript_cost failed", exc_info=True)

    async def connect(self) -> None:
        """Build the client and arm the buffer (Gemini needs no live socket)."""
        self._ensure_client()
        self._running = True
        self._buffer = bytearray()

    async def send_audio(self, audio_chunk: bytes) -> None:
        """Buffer a PCM16 chunk; transcription happens at turn end."""
        if not self._running or not audio_chunk:
            return
        self._audio_bytes_sent += len(audio_chunk)
        self._buffer.extend(audio_chunk)

    async def finalize(self) -> None:
        """Turn ended (VAD speech-end): transcribe the buffered utterance.

        The pipeline's ``speech_end`` fast-path duck-types ``stt.finalize``;
        for this adapter it is the ONLY trigger, since a windowed upload would
        split one utterance across requests and destroy the tone judgement.
        """
        if not self._buffer:
            return
        self._spawn_transcription(self._flush())

    async def receive_transcripts(self) -> AsyncIterator[Transcript]:
        """Yield transcripts as the buffered turns come back from the model.

        Keeps draining after ``_running`` goes False so transcripts flushed by
        :meth:`close` are not dropped; :meth:`close` enqueues a ``None``
        sentinel once flushing is complete, which ends this generator.
        """
        while True:
            try:
                transcript = await asyncio.wait_for(
                    self._transcript_queue.get(), timeout=_QUEUE_POLL_SECONDS
                )
            except asyncio.TimeoutError:
                if not self._running and self._transcript_queue.empty():
                    # close() was never called (e.g. unit-test tear-down) —
                    # give up gracefully instead of looping forever.
                    break
                continue
            if transcript is None:
                break
            self._record_transcript_cost()
            yield transcript

    async def close(self) -> None:
        """Flush the trailing utterance, await in-flight requests, then stop."""
        if self._buffer:
            self._spawn_transcription(self._flush())
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)
        self._running = False
        await self._transcript_queue.put(None)
        self._client = None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _flush(self) -> bytes:
        pcm = bytes(self._buffer)
        self._buffer.clear()
        return pcm

    def _spawn_transcription(self, pcm: bytes) -> None:
        """Run one transcription in the background so audio keeps flowing."""
        task = asyncio.create_task(self._transcribe_and_enqueue(pcm))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _transcribe_and_enqueue(self, pcm: bytes) -> None:
        transcript = await self._transcribe(pcm)
        if transcript is not None:
            await self._transcript_queue.put(transcript)

    async def _transcribe(self, pcm: bytes) -> Transcript | None:
        """Send one utterance to Gemini and map the reply onto a transcript."""
        wav = _pcm_to_wav(pcm, self.sample_rate)
        try:
            client = self._ensure_client()
            response = await client.aio.models.generate_content(
                model=self.model,
                contents=[
                    {
                        "role": "user",
                        "parts": [
                            {"text": TONE_PROMPT},
                            {
                                "inline_data": {
                                    "mime_type": "audio/wav",
                                    "data": wav,
                                }
                            },
                        ],
                    }
                ],
                config={
                    "temperature": GEMINI_STT_TEMPERATURE,
                    "max_output_tokens": GEMINI_STT_MAX_OUTPUT_TOKENS,
                },
            )
        except Exception as exc:  # noqa: BLE001 - never kill the call on STT
            logger.exception("Gemini STT transcription error: %s", exc)
            return None

        text = _extract_text(response).strip()
        if not text:
            return None
        # One request covers exactly one turn, so the result is both the final
        # transcript and the end of the utterance.
        return Transcript(
            text=text,
            is_final=True,
            confidence=1.0,
            speech_final=True,
        )
