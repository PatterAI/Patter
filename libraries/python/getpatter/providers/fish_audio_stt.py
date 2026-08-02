"""Fish Audio Speech-to-Text for the Patter SDK — buffered batch adapter.

Beta: validated against the Fish Audio API spec (docs.fish.audio); not yet
exercised against a live phone call.

Fish exposes transcription as a **batch** endpoint only
(``POST https://api.fish.audio/v1/asr``) — there is no streaming socket. This
adapter therefore follows the same buffer-and-POST shape as
:class:`~getpatter.providers.whisper_stt.WhisperSTT`: incoming PCM is
accumulated, uploaded as a WAV once the buffer fills, and the resulting text is
emitted as a final :class:`~getpatter.providers.base.Transcript`.

Latency consequence: transcripts arrive in ~2 s windows rather than as interim
partials. For latency-sensitive agents prefer a genuinely streaming STT
(Deepgram, Soniox, AssemblyAI, Speechmatics). Use this adapter when you want a
single vendor for both directions of the call, or for Fish's language coverage.

Credential: ``FISH_AUDIO_API_KEY`` env var / ``api_key`` argument. Auth is an
``Authorization: Bearer <key>`` header.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import wave
from typing import Any, AsyncIterator, ClassVar, Optional

from getpatter.providers.base import STTProvider, Transcript
from getpatter.providers.fish_audio_tts import FISH_AUDIO_API_ORIGIN

logger = logging.getLogger("getpatter.providers.fish_audio_stt")

# Lazy import: aiohttp is declared as an optional dep for this provider
# (shared ``[fish_audio]`` extra with the Fish TTS adapters).
try:  # pragma: no cover - trivial import guard
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore

FISH_AUDIO_ASR_URL = f"{FISH_AUDIO_API_ORIGIN}/v1/asr"

# The adapter always uploads 16 kHz / 16-bit / mono WAV.
STT_SAMPLE_RATE = 16000
_BYTES_PER_SECOND = STT_SAMPLE_RATE * 2

# ~2 s of audio per upload. Fish rejects clips under 1 second, so a 1 s window
# (what WhisperSTT uses) would sit exactly on the boundary; 2 s keeps every
# steady-state upload comfortably inside the accepted range and halves the
# request count.
BUFFER_SIZE_BYTES = _BYTES_PER_SECOND * 2

# Fish's documented minimum clip length. Tails shorter than this are padded
# with digital silence rather than dropped — see :meth:`close`.
MIN_AUDIO_BYTES = _BYTES_PER_SECOND

# Fish's documented per-request ceiling (20 MB). A 2 s window is ~64 KB, so this
# only ever guards against a pathological buffer.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


class FishAudioSTT(STTProvider):
    """Fish Audio ASR adapter — buffers PCM audio and transcribes in windows.

    Compatible with the :class:`~getpatter.providers.base.STTProvider` interface
    so it can be swapped into pipeline mode without changing calling code.

    Args:
        api_key: Fish Audio API key. Falls back to ``FISH_AUDIO_API_KEY``.
        language: ISO language code (e.g. ``"en"``). Pass ``None`` to let Fish
            auto-detect — Fish's own default.
        ignore_timestamps: ``True`` (default) skips segment timing, which Fish
            documents as the lower-latency path for clips under 30 s. Set
            ``False`` to receive per-segment ``start`` / ``end`` on
            :attr:`Transcript.words`.
        buffer_size_bytes: Bytes of 16 kHz PCM16 to accumulate before each
            upload. Default ~2 s.
    """

    #: Stable pricing/dashboard key — read by stream-handler/metrics.
    provider_key: ClassVar[str] = "fish_audio_stt"

    def __init__(
        self,
        api_key: Optional[str] = None,
        language: Optional[str] = "en",
        *,
        ignore_timestamps: bool = True,
        buffer_size_bytes: int = BUFFER_SIZE_BYTES,
        base_url: str = FISH_AUDIO_ASR_URL,
        session: Optional["aiohttp.ClientSession"] = None,
    ) -> None:
        if aiohttp is None:
            raise ImportError(
                "aiohttp is required for FishAudioSTT. "
                "Install with: pip install getpatter[fish_audio]"
            )

        resolved_key = api_key or os.environ.get("FISH_AUDIO_API_KEY")
        if not resolved_key:
            raise ValueError(
                "Fish Audio STT requires an api_key. Pass api_key='...' or "
                "set FISH_AUDIO_API_KEY in the environment."
            )

        self.api_key = resolved_key
        self.language = language
        self.ignore_timestamps = ignore_timestamps
        self.buffer_size_bytes = buffer_size_bytes
        self.base_url = base_url
        # Declared so the observability layer can compute audio seconds.
        self.sample_rate = STT_SAMPLE_RATE
        self.encoding = "linear16"

        self._buffer = bytearray()
        self._audio_bytes_sent: int = 0
        # Queue holds Transcript items; None is a sentinel that signals the
        # generator to stop after draining all real transcripts.
        self._transcript_queue: asyncio.Queue[Transcript | None] = asyncio.Queue()
        self._running = False
        self._pending: set[asyncio.Task] = set()
        self._owns_session = session is None
        self._session = session

    def __repr__(self) -> str:
        # Never leak the API key in repr / logs.
        return (
            f"FishAudioSTT(language={self.language!r}, "
            f"ignore_timestamps={self.ignore_timestamps})"
        )

    @classmethod
    def for_twilio(
        cls,
        api_key: Optional[str] = None,
        language: Optional[str] = "en",
        **kwargs: Any,
    ) -> "FishAudioSTT":
        """Factory mirroring the TS ``forTwilio`` helper.

        Twilio delivers mu-law @ 8 kHz which the upstream transcoder converts to
        PCM16 @ 16 kHz before it reaches this adapter, so no extra config is
        needed — the factory exists for API parity.
        """
        return cls(api_key=api_key, language=language, **kwargs)

    def _ensure_session(self) -> "aiohttp.ClientSession":
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    def _record_transcript_cost(self) -> None:
        """Emit ``patter.cost.stt_seconds`` for the transcribed audio."""
        try:
            from getpatter.observability.attributes import record_patter_attrs

            seconds = self._audio_bytes_sent / float(_BYTES_PER_SECOND)
            record_patter_attrs(
                {
                    "patter.cost.stt_seconds": seconds,
                    "patter.stt.provider": "fish_audio",
                }
            )
            self._audio_bytes_sent = 0
        except Exception:  # pragma: no cover — defense in depth
            logger.debug("_record_transcript_cost failed", exc_info=True)

    async def connect(self) -> None:
        """Initialise the adapter (Fish ASR needs no persistent connection)."""
        self._running = True
        self._buffer = bytearray()

    async def send_audio(self, audio_chunk: bytes) -> None:
        """Buffer incoming PCM audio and transcribe when the window fills."""
        self._audio_bytes_sent += len(audio_chunk)
        self._buffer.extend(audio_chunk)
        if len(self._buffer) >= self.buffer_size_bytes:
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
        """Upload one PCM window to ``/v1/asr`` and return the transcript."""
        if len(pcm_data) > MAX_UPLOAD_BYTES:
            logger.warning(
                "Fish Audio STT buffer is %d bytes, over the %d-byte per-request "
                "limit; truncating to the most recent audio.",
                len(pcm_data),
                MAX_UPLOAD_BYTES,
            )
            pcm_data = pcm_data[-MAX_UPLOAD_BYTES:]

        wav_bytes = _pcm_to_wav(pcm_data, STT_SAMPLE_RATE)
        form = aiohttp.FormData()
        form.add_field(
            "audio", wav_bytes, filename="audio.wav", content_type="audio/wav"
        )
        if self.language:
            form.add_field("language", self.language)
        form.add_field(
            "ignore_timestamps", "true" if self.ignore_timestamps else "false"
        )

        try:
            session = self._ensure_session()
            async with session.post(
                self.base_url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                data=form,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error("Fish Audio STT error %d: %s", resp.status, body[:500])
                    return None
                payload = await resp.json()
        except Exception as exc:  # noqa: BLE001 - never kill the call on STT
            logger.exception("Fish Audio STT transcription error: %s", exc)
            return None

        return _parse_asr_response(payload)

    async def receive_transcripts(self) -> AsyncIterator[Transcript]:
        """Yield transcripts as they arrive.

        Keeps draining after ``_running`` goes False so transcripts flushed by
        :meth:`close` (trailing buffer + in-flight uploads) are not dropped;
        :meth:`close` enqueues a ``None`` sentinel once flushing is complete,
        which ends this generator.
        """
        while True:
            try:
                transcript = await asyncio.wait_for(
                    self._transcript_queue.get(), timeout=0.1
                )
            except asyncio.TimeoutError:
                if not self._running and self._transcript_queue.empty():
                    # close() was never called (e.g. unit-test tear-down) —
                    # give up gracefully instead of looping forever.
                    break
                continue
            if transcript is None:
                break
            if transcript.is_final:
                self._record_transcript_cost()
            yield transcript

    async def close(self) -> None:
        """Flush the remaining buffer and release the HTTP session.

        Fish rejects clips shorter than 1 second, so a short tail is padded with
        digital silence up to the minimum rather than dropped — the alternative
        would silently lose the last words of an utterance, the exact bug that
        was fixed on the Whisper adapter.
        """
        if len(self._buffer) > 0:
            tail = bytes(self._buffer)
            if len(tail) < MIN_AUDIO_BYTES:
                tail = tail + b"\x00" * (MIN_AUDIO_BYTES - len(tail))
            transcript = await self._transcribe_buffer(tail)
            if transcript:
                await self._transcript_queue.put(transcript)
        self._buffer.clear()
        # Wait for in-flight uploads so their results land before the sentinel.
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)
        self._running = False
        await self._transcript_queue.put(None)
        if self._session is not None and self._owns_session:
            await self._session.close()
            self._session = None


def _pcm_to_wav(pcm_data: bytes, sample_rate: int) -> bytes:
    """Wrap raw PCM16 mono samples in a WAV container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)
    return buf.getvalue()


def _parse_asr_response(payload: Any) -> Transcript | None:
    """Map a ``/v1/asr`` response onto a :class:`Transcript`.

    Fish returns ``{"text": str, "duration": float, "segments": [...]}``.
    Segments are surfaced on :attr:`Transcript.words` when the caller asked for
    timestamps; Fish reports no confidence score, so ``confidence`` is 1.0.
    """
    if not isinstance(payload, dict):
        return None
    text = (payload.get("text") or "").strip()
    if not text:
        return None
    segments = payload.get("segments")
    words: tuple[dict[str, Any], ...] = ()
    if isinstance(segments, list):
        words = tuple(seg for seg in segments if isinstance(seg, dict))
    return Transcript(
        text=text,
        is_final=True,
        confidence=1.0,
        speech_final=True,
        words=words,
    )
