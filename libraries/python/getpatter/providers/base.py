"""Base classes for all Patter providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, AsyncIterator, Literal


# === STT ===


@dataclass(frozen=True)
class Transcript:
    """A transcription result emitted by an :class:`STTProvider`.

    ``is_final`` distinguishes provisional partials from finalised utterances;
    additional fields carry provider-specific hints (``speech_final``,
    ``event_type``) and metadata used for cost reconciliation.

    This dataclass is frozen (immutable). ``words`` uses a tuple so the
    container is hashable; the inner dicts remain mutable (shallow immutability
    only — a known limitation documented in the immutability rule).
    """

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
    words: tuple[dict[str, Any], ...] = ()
    # Type of event from the provider. ``Results`` is the default transcript
    # frame; ``UtteranceEnd`` and ``SpeechStarted`` are VAD events emitted
    # by Deepgram when ``vad_events=true``.
    event_type: Literal["Results", "UtteranceEnd", "SpeechStarted"] = "Results"


class STTProvider(ABC):
    """Abstract base class for streaming speech-to-text providers."""

    def __init_subclass__(cls, **kwargs: object) -> None:
        """Capture constructor arguments so :meth:`clone` works generically.

        STT adapters are stateful per-connection objects, but the documented
        usage pattern hands ONE instance to ONE agent served for MANY calls —
        concurrent calls then share a socket/queue and corrupt each other
        (cross-call transcript bleed). The stream handlers clone the
        configured instance per call via :meth:`clone`; this hook wraps every
        subclass ``__init__`` to record the ORIGINAL construction arguments
        (outermost call wins through inheritance chains) with zero
        per-provider code.
        """
        super().__init_subclass__(**kwargs)
        original_init = cls.__dict__.get("__init__")
        if original_init is None or getattr(
            original_init, "_patter_captures_ctor", False
        ):
            return

        import functools

        @functools.wraps(original_init)
        def _capturing_init(self, *args, **kw):  # type: ignore[no-untyped-def]
            if not hasattr(self, "_patter_ctor_args"):
                self._patter_ctor_args = (args, dict(kw))
            original_init(self, *args, **kw)

        _capturing_init._patter_captures_ctor = True  # type: ignore[attr-defined]
        cls.__init__ = _capturing_init  # type: ignore[method-assign]

    def clone(self) -> "STTProvider":
        """Return a FRESH adapter built with this instance's constructor args.

        Called by the stream handlers so every call gets its own connection
        state. Subclasses with non-replayable constructor arguments may
        override. Raises ``TypeError`` if construction arguments were never
        captured (e.g. an instance built via ``object.__new__``).
        """
        captured = getattr(self, "_patter_ctor_args", None)
        if captured is None:
            raise TypeError(
                f"{type(self).__name__}.clone(): constructor arguments were "
                "not captured; override clone() for this adapter."
            )
        args, kw = captured
        return type(self)(*args, **kw)

    @abstractmethod
    async def connect(self) -> None:
        """Open the provider connection (WebSocket, gRPC, etc.)."""

    @abstractmethod
    async def send_audio(self, audio_chunk: bytes) -> None:
        """Forward a single PCM/mulaw audio chunk to the provider."""

    @abstractmethod
    async def receive_transcripts(self) -> AsyncIterator[Transcript]:
        """Yield :class:`Transcript` events as they arrive from the provider."""

    @abstractmethod
    async def close(self) -> None:
        """Close the provider connection and release resources."""

    async def warmup(self) -> None:
        """Best-effort pre-call connection / DNS / TLS warmup.

        Default implementation is a no-op. Providers can override to dial
        open a persistent connection, prime DNS, or kick off a TLS handshake
        ahead of the actual ``connect()`` call placed by the stream handler
        when the carrier reports ``answered``.

        Called once per outbound call from :meth:`Patter.call` when the
        agent has ``prewarm=True`` (the default). Failures are logged at
        DEBUG and never abort the call — this is purely a latency win.

        Mirrors ``warmup()`` on :class:`TTSProvider` and the
        :class:`LLMProvider` protocol. See ``Agent.prewarm`` for the
        feature rationale.
        """
        return None


# === TTS ===


class TTSProvider(ABC):
    """Abstract base class for streaming text-to-speech providers."""

    @abstractmethod
    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Synthesize *text*, yielding raw audio bytes as they become available."""

    @abstractmethod
    async def close(self) -> None:
        """Close the TTS connection and release resources."""

    async def warmup(self) -> None:
        """Best-effort pre-call connection / DNS / TLS warmup.

        Default implementation is a no-op. Providers can override to prime
        DNS / TLS / HTTP/2 ahead of the first ``synthesize()`` call so the
        TTS first-byte latency is dominated by inference time only.

        Called once per outbound call from :meth:`Patter.call` when the
        agent has ``prewarm=True`` (the default). Failures are logged at
        DEBUG and never abort the call.

        See ``Agent.prewarm`` for the feature rationale and
        :class:`STTProvider.warmup` for the parallel STT method.
        """
        return None


# === Telephony ===


@dataclass(frozen=True)
class CallInfo:
    """Lightweight descriptor for an active call (id, parties, direction)."""

    call_id: str
    caller: str
    callee: str
    direction: str


class TelephonyProvider(ABC):
    """Abstract base class for carrier adapters (Twilio, Telnyx, ...)."""

    @abstractmethod
    async def provision_number(self, country: str) -> str:
        """Buy or reserve a phone number from the carrier in the given ISO country."""

    @abstractmethod
    async def configure_number(self, number: str, webhook_url: str) -> None:
        """Point the carrier-side webhook for *number* at *webhook_url*."""

    @abstractmethod
    async def initiate_call(
        self, from_number: str, to_number: str, stream_url: str
    ) -> str:
        """Place an outbound call and bridge the media stream to *stream_url*."""

    @abstractmethod
    async def end_call(self, call_id: str) -> None:
        """Hang up the named call via the carrier API."""


# === VAD (Voice Activity Detection) ===


@dataclass(frozen=True)
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
    async def close(self) -> None:
        """Release any model or backend resources held by the VAD."""

    def reset(self) -> None:
        """Reset all per-utterance state so the next ``process_frame`` starts
        from a clean SILENCE state.

        Default implementation is a no-op so existing providers compile
        unchanged. Implementations that hold streaming detector state
        (Silero RNN context, smoothing filters) should override this to
        wipe the state between agent turns — without it, PSTN echo can
        keep the detector "stuck" in SPEECH for the whole agent turn and
        block barge-in on the next user utterance (one-shot barge-in
        bug).
        """
        return None


# === Semantic turn detection (end-of-utterance) ===


class TurnDetectorProvider(ABC):
    """Semantic end-of-utterance (turn) detector.

    Predicts whether the caller has FINISHED their turn — as opposed to a
    VAD, which only reports whether they are currently producing sound.
    Implementations include :class:`~getpatter.providers.smart_turn.SmartTurnDetector`
    (pipecat-ai smart-turn v3, ONNX). Used by :class:`~getpatter.models.Agent`
    via the ``turn_detector`` field; integrated in ``PipelineStreamHandler``
    on the VAD ``speech_end`` edge to defer the STT finalize until the model
    agrees the turn is complete (bounded by ``Agent.max_semantic_hold_ms``).
    """

    @property
    @abstractmethod
    def threshold(self) -> float:
        """End-of-turn probability at/above which the turn is complete."""

    @abstractmethod
    async def predict(self, pcm16_16k_window: bytes) -> float:
        """Return the end-of-turn probability in ``[0, 1]`` for the window.

        ``pcm16_16k_window`` is mono int16 little-endian PCM at 16 kHz
        covering the most recent seconds of caller audio (the handler
        keeps a rolling ~8 s buffer).
        """

    @abstractmethod
    async def close(self) -> None:
        """Release any model or backend resources held by the detector."""


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
    async def close(self) -> None:
        """Release any backend resources held by the filter."""


# === Background audio (hold music, ambient cues) ===


class BackgroundAudioPlayer(ABC):
    """Mixes background audio (hold music, thinking cues) with TTS output.

    Implementations are expected to manage their own lifecycle and mix PCM
    chunks with the agent's outbound audio stream via ``mix(pcm)``.
    """

    @abstractmethod
    async def start(self) -> None:
        """Decode the background source and arm the mixer."""

    @abstractmethod
    async def mix(self, agent_pcm: bytes, sample_rate: int) -> bytes:
        """Mix the given agent PCM with the current background source."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop playback and release decoded buffers."""
