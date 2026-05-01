"""Base classes for all Patter providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal


# === STT ===

@dataclass
class Transcript:
    text: str
    is_final: bool
    confidence: float = 0.0
    # Deepgram (and other providers) emit a faster end-of-utterance hint via
    # ``speech_final``. Kept separate from ``is_final`` so callers can gate
    # turn-ending on either signal independently.
    speech_final: bool = False
    # Set by Deepgram on the Results frame produced in response to a
    # ``Finalize`` control message (used by :meth:`close` to flush trailing
    # partials before tearing down the socket).
    from_finalize: bool = False
    # Provider-side request id (e.g. Deepgram's ``request_id``) — useful for
    # post-call cost reconciliation and tracing.
    request_id: str | None = None
    # Per-word timings/metadata when the provider emits them. Shape is
    # provider-specific; callers that consume it should introspect carefully.
    words: list[dict[str, Any]] = field(default_factory=list)
    # Type of event from the provider. ``Results`` is the default transcript
    # frame; ``UtteranceEnd`` and ``SpeechStarted`` are VAD events emitted
    # by Deepgram when ``vad_events=true``.
    event_type: Literal["Results", "UtteranceEnd", "SpeechStarted"] = "Results"


class STTProvider(ABC):
    @abstractmethod
    async def connect(self) -> None: ...
    @abstractmethod
    async def send_audio(self, audio_chunk: bytes) -> None: ...
    @abstractmethod
    async def receive_transcripts(self) -> AsyncIterator[Transcript]: ...
    @abstractmethod
    async def close(self) -> None: ...


# === TTS ===

class TTSProvider(ABC):
    @abstractmethod
    async def synthesize(self, text: str) -> AsyncIterator[bytes]: ...
    @abstractmethod
    async def close(self) -> None: ...


# === Telephony ===

@dataclass
class CallInfo:
    call_id: str
    caller: str
    callee: str
    direction: str


class TelephonyProvider(ABC):
    @abstractmethod
    async def provision_number(self, country: str) -> str: ...
    @abstractmethod
    async def configure_number(self, number: str, webhook_url: str) -> None: ...
    @abstractmethod
    async def initiate_call(self, from_number: str, to_number: str, stream_url: str) -> str: ...
    @abstractmethod
    async def end_call(self, call_id: str) -> None: ...


# === VAD (Voice Activity Detection) ===

@dataclass
class VADEvent:
    """Voice activity event emitted by a VADProvider.

    Attributes:
        type: ``speech_start`` when speech begins, ``speech_end`` when it ends,
            ``silence`` while no speech is detected.
        confidence: Model confidence in [0.0, 1.0].
        duration_ms: Duration of the frame or span in milliseconds.
    """

    type: Literal["speech_start", "speech_end", "silence"]
    confidence: float = 0.0
    duration_ms: float = 0.0


class VADProvider(ABC):
    """Server-side voice activity detector.

    Receives PCM audio frames and emits VADEvents. Implementations include
    Silero (acoustic, ONNX-based). Used by :class:`~getpatter.models.Agent`
    via the ``vad`` field; integrated in ``PipelineStreamHandler`` before STT
    to gate empty-audio frames.
    """

    @abstractmethod
    async def process_frame(
        self, pcm_chunk: bytes, sample_rate: int
    ) -> VADEvent | None:
        """Process a PCM frame. Returns an event when state changes, else None."""

    @abstractmethod
    async def close(self) -> None: ...


# === Audio filter (noise cancellation, gain, EQ) ===

class AudioFilter(ABC):
    """Pre-STT audio filter.

    Used for noise cancellation (Krisp, DeepFilterNet, rnnoise). Integrated
    in ``PipelineStreamHandler.on_audio_received`` before VAD and STT.
    """

    @abstractmethod
    async def process(self, pcm_chunk: bytes, sample_rate: int) -> bytes:
        """Transform input PCM, return filtered PCM (same sample rate)."""

    @abstractmethod
    async def close(self) -> None: ...


# === Background audio (hold music, ambient cues) ===

class BackgroundAudioPlayer(ABC):
    """Mixes background audio (hold music, thinking cues) with TTS output.

    Implementations are expected to manage their own lifecycle and mix PCM
    chunks with the agent's outbound audio stream via ``mix(pcm)``.
    """

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def mix(self, agent_pcm: bytes, sample_rate: int) -> bytes:
        """Mix the given agent PCM with the current background source."""

    @abstractmethod
    async def stop(self) -> None: ...
