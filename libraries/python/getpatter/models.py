"""Public dataclasses and runtime models for the Patter SDK.

These are the primary types users construct (``Agent``, ``Guardrail``,
``PipelineHooks``, ``STTConfig``, ``TTSConfig``) and observe
(``CallMetrics``, ``LatencyBreakdown``, ``CostBreakdown``, ``CallControl``).
All public configs are frozen dataclasses for immutability — see
``.claude/rules/immutability.md``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Literal

if TYPE_CHECKING:
    from getpatter.providers.base import AudioFilter, BackgroundAudioPlayer, VADProvider
    from getpatter.services.llm_loop import LLMProvider

logger = logging.getLogger("getpatter")

# Closed set of provider modes the SDK dispatches on. Matches the TypeScript
# string union ``'openai_realtime' | 'elevenlabs_convai' | 'pipeline'`` in
# ``types.ts``. Tightened from a free ``str`` so editors autocomplete and
# typos surface at type-check time instead of at call time.
ProviderMode = Literal["openai_realtime", "elevenlabs_convai", "pipeline"]


@dataclass(frozen=True)
class Guardrail:
    """Output guardrail — filters AI responses before TTS.

    Args:
        name: Identifier used in log messages when the guardrail fires.
        check: Optional callable ``(text: str) -> bool`` that returns ``True``
            when the response should be blocked.
        blocked_terms: Optional list of words/phrases; any match blocks the
            response (case-insensitive substring check).
        replacement: What the agent says instead when a response is blocked.
    """

    name: str
    check: Callable[[str], bool] | None = None
    blocked_terms: list[str] | None = None
    replacement: str = "I'm sorry, I can't respond to that."


@dataclass(frozen=True)
class HookContext:
    """Context passed to pipeline hooks."""

    call_id: str
    caller: str
    callee: str
    history: tuple[dict, ...] = ()


@dataclass(frozen=True)
class PipelineHooks:
    """Pipeline hooks for intercepting data at each stage (pipeline mode only).

    Each hook receives the data and a :class:`HookContext`. Return ``None``
    to skip the downstream step (or, for ``before_llm`` / ``after_llm``,
    keep the original value). Hooks may be sync or async.

    Attributes:
        before_send_to_stt: Called with the raw PCM audio chunk before it is
            forwarded to the STT provider. Return ``None`` to drop the chunk
            (e.g., to implement custom VAD gating).
        after_transcribe: Called after STT, before LLM. Return ``None`` to skip turn.
        before_llm: Called with the messages list before the LLM call.
            Return ``None`` to keep them, or return a new list to replace
            (useful for prompt injection, message filtering, RAG augmentation).
        after_llm: Called with the final assistant text after the LLM stream
            completes. Return ``None`` to keep, or return a new string to
            replace (useful for output validation, redaction, post-processing).
        before_synthesize: Called before TTS, per-sentence in streaming mode.
            Return ``None`` to skip TTS for this sentence.
        after_synthesize: Called after TTS produces an audio chunk.
            Return ``None`` to discard the chunk.
    """

    before_send_to_stt: Callable | None = None
    after_transcribe: Callable | None = None
    before_llm: Callable | None = None
    after_llm: Callable | None = None
    before_synthesize: Callable | None = None
    after_synthesize: Callable | None = None


@dataclass(frozen=True)
class Agent:
    """Configuration for a local-mode voice AI agent.

    Several fields are also carried by engine markers
    (``engines.openai.Realtime``, ``engines.elevenlabs.ConvAI``) and by the
    server-instantiated adapters (``providers.openai_realtime.OpenAIRealtimeAdapter``,
    ``providers.elevenlabs_convai.ElevenLabsConvAIAdapter``). When the same
    setting is set in two places, precedence is:

    1. **Explicit kwarg on** ``Patter.agent(voice=..., model=..., language=...)``
       always wins.
    2. Otherwise, when an ``engine=`` is passed, the engine's value populates
       the Agent (see ``Patter._unpack_engine``).
    3. Otherwise, the Agent default is used.

    The server passes the resolved Agent down to the adapter at call time, so
    the adapter's own ``voice``/``model``/``language`` arguments mirror the
    Agent's — they are not independent overrides at runtime.
    """

    system_prompt: str
    voice: str = "alloy"
    model: str = "gpt-4o-mini-realtime-preview"
    language: str = "en"
    first_message: str = ""
    tools: list[dict] | None = None
    provider: ProviderMode = "openai_realtime"
    stt: STTConfig | None = None  # which STT provider to use in pipeline mode
    tts: TTSConfig | None = None  # which TTS provider to use in pipeline mode
    variables: dict | None = (
        None  # Dynamic variables for ``{placeholder}`` substitution in system_prompt
    )
    guardrails: list[Guardrail | dict] | None = (
        None  # List of Guardrail objects or guardrail dicts
    )
    hooks: PipelineHooks | None = None  # Pipeline hooks for pipeline mode
    text_transforms: list[Callable] | None = (
        None  # Text transforms applied to LLM output before TTS
    )
    vad: "VADProvider | None" = (
        None  # Optional server-side VAD (e.g., Silero) — pipeline mode only
    )
    audio_filter: "AudioFilter | None" = (
        None  # Optional pre-STT audio filter (noise cancel) — pipeline mode only
    )
    background_audio: "BackgroundAudioPlayer | None" = (
        None  # Optional background audio mixer — pipeline mode only
    )
    llm: "LLMProvider | None" = (
        None  # Optional built-in LLM provider for pipeline mode (e.g., AnthropicLLM())
    )
    # Minimum sustained voice (ms) before treating caller audio as a barge-in
    # and interrupting TTS. ``0`` disables barge-in entirely — useful on noisy
    # links (ngrok tunnels, speakerphone) where the agent can hear itself.
    barge_in_threshold_ms: int = 300
    # When ``True``, the sentence chunker emits the first clause of each
    # response on a soft punctuation boundary (",", em-dash, en-dash) once
    # ~40 chars have accumulated. Saves 200-500 ms TTFA on the first
    # sentence of each turn at the cost of slightly clipping prosody on the
    # very first chunk. Hard-disabled when ``language`` starts with ``"it"``
    # (Italian decimal comma would split mid-number). Default: ``False``.
    aggressive_first_flush: bool = False
    # Patter prepends a default phone-friendly preamble to ``system_prompt``
    # so the LLM responds in spoken-language style (no markdown, emojis,
    # bullet lists, code blocks; numbers and dates spelled out; replies kept
    # short). Set to ``True`` to disable and ship ``system_prompt`` verbatim.
    disable_phone_preamble: bool = False


@dataclass(frozen=True)
class CallEvent:
    """Call lifecycle event."""

    call_id: str
    caller: str = ""
    callee: str = ""
    direction: str = ""


@dataclass(frozen=True)
class IncomingMessage:
    """Inbound user utterance forwarded to ``on_message`` handlers."""

    text: str
    call_id: str
    caller: str


@dataclass(frozen=True)
class STTConfig:
    """Pipeline-mode STT provider selection (provider key + credentials + options)."""

    provider: str
    api_key: str
    language: str = "en"
    # Provider-specific tuning knobs (e.g. Deepgram endpointing). Unknown keys
    # are silently ignored so older SDK versions stay forward-compatible.
    options: dict | None = None

    def to_dict(self) -> dict:
        """Serialize to a plain dict for transport / logging."""
        out = {
            "provider": self.provider,
            "api_key": self.api_key,
            "language": self.language,
        }
        if self.options:
            out["options"] = dict(self.options)
        return out


@dataclass(frozen=True)
class TTSConfig:
    """Pipeline-mode TTS provider selection (provider key + credentials + voice)."""

    provider: str
    api_key: str
    voice: str = "alloy"
    options: dict | None = None

    def to_dict(self) -> dict:
        """Serialize to a plain dict for transport / logging."""
        out = {"provider": self.provider, "api_key": self.api_key, "voice": self.voice}
        if self.options:
            out["options"] = dict(self.options)
        return out


@dataclass(frozen=True)
class CostBreakdown:
    """Per-call cost breakdown by segment (USD)."""

    stt: float = 0.0
    tts: float = 0.0
    llm: float = 0.0
    telephony: float = 0.0
    total: float = 0.0
    # Amount saved on LLM cost thanks to OpenAI Realtime prompt caching.
    # ``llm`` above is the net cost AFTER this discount. Dashboards can
    # render "saved $X (pct%)" next to the LLM line when > 0.
    llm_cached_savings: float = 0.0


@dataclass(frozen=True)
class LatencyBreakdown:
    """Per-turn latency breakdown (milliseconds)."""

    stt_ms: float = 0.0
    llm_ms: float = 0.0
    tts_ms: float = 0.0
    total_ms: float = 0.0
    # Time-to-first-token for the LLM (stt_complete → first streaming token).
    # ``None`` in Realtime / non-streaming paths where the LLM doesn't expose
    # TTFT separately. Populated by ``CallMetricsAccumulator`` from
    # ``record_llm_first_token``.
    llm_ttft_ms: float | None = None
    # Total LLM generation time (stt_complete → llm_complete). Distinct from
    # ``llm_ms`` (TTFT-style first-token latency) — this captures the full
    # token-stream duration which is useful for cost / throughput analysis.
    llm_total_ms: float | None = None
    # Endpoint latency: time from end-of-user-speech (VAD stop or STT
    # ``speech_final``) to LLM dispatch. Captures the silence-detection +
    # transcript-finalization gap. ``None`` when the source signal is missing.
    endpoint_ms: float | None = None
    # Barge-in latency: time from user-interrupt detection to TTS playback
    # actually halting (i.e. after ``audio_sender.send_clear()`` returned).
    # ``None`` outside of an interrupted turn.
    bargein_ms: float | None = None
    # Total TTS time: LLM-first-token (or first-sentence boundary) to last
    # TTS audio byte sent. ``None`` when TTS never completed.
    tts_total_ms: float | None = None
    # **User-perceived agent response latency** — the metric to watch on
    # SLO / p95 dashboards. Computed as ``endpoint_ms + llm_ttft_ms +
    # tts_ms`` when all three signals are available, ``None`` otherwise.
    # Unlike ``total_ms`` (which includes the user's entire utterance and
    # therefore grows with how long they spoke), ``agent_response_ms``
    # isolates the system-controlled latency: silence detection + LLM TTFT
    # + TTS first byte.
    agent_response_ms: float | None = None


@dataclass(frozen=True)
class TurnMetrics:
    """Metrics for a single conversation turn."""

    turn_index: int
    user_text: str
    agent_text: str
    latency: LatencyBreakdown
    stt_audio_seconds: float = 0.0
    tts_characters: int = 0
    timestamp: float = 0.0


@dataclass(frozen=True)
class CallMetrics:
    """Accumulated metrics for an entire call."""

    call_id: str
    duration_seconds: float
    turns: tuple[TurnMetrics, ...]
    cost: CostBreakdown
    latency_avg: LatencyBreakdown
    latency_p95: LatencyBreakdown
    provider_mode: str
    stt_provider: str = ""
    tts_provider: str = ""
    llm_provider: str = ""
    telephony_provider: str = ""
    # Additional percentiles exposed for richer latency dashboards.
    # Default to zero so older consumers still construct CallMetrics cleanly.
    latency_p50: LatencyBreakdown = field(default_factory=LatencyBreakdown)
    latency_p90: LatencyBreakdown = field(default_factory=LatencyBreakdown)
    latency_p99: LatencyBreakdown = field(default_factory=LatencyBreakdown)


class CallControl:
    """In-call control interface passed to ``on_message`` handlers.

    Allows the handler to transfer the call, hang up, or send DTMF tones
    without needing direct access to the telephony provider.

    Usage::

        async def handle(data, call: CallControl):
            if needs_transfer:
                await call.transfer("+15551234567")
            elif is_done:
                await call.hangup()
            else:
                return "Hello!"
    """

    def __init__(
        self,
        call_id: str,
        caller: str,
        callee: str,
        telephony_provider: str,
        *,
        _transfer_fn=None,
        _hangup_fn=None,
        _send_dtmf_fn=None,
    ):
        self.call_id = call_id
        self.caller = caller
        self.callee = callee
        self.telephony_provider = telephony_provider
        self._transfer_fn = _transfer_fn
        self._hangup_fn = _hangup_fn
        self._send_dtmf_fn = _send_dtmf_fn
        self._transferred = asyncio.Event()
        self._hung_up = asyncio.Event()

    @property
    def is_transferred(self) -> bool:
        """True if transfer() was called."""
        return self._transferred.is_set()

    @property
    def is_hung_up(self) -> bool:
        """True if hangup() was called."""
        return self._hung_up.is_set()

    @property
    def ended(self) -> bool:
        """True if transfer() or hangup() was called."""
        return self._transferred.is_set() or self._hung_up.is_set()

    async def transfer(self, number: str) -> None:
        """Transfer the call to another phone number (E.164 format)."""
        if self._transfer_fn is not None:
            await self._transfer_fn(number)
            self._transferred.set()
        else:
            logger.warning("transfer() not available for this provider mode")

    async def hangup(self) -> None:
        """End the call."""
        if self._hangup_fn is not None:
            await self._hangup_fn()
            self._hung_up.set()
        else:
            logger.warning("hangup() not available for this provider mode")

    async def send_dtmf(self, digits: str, *, delay_ms: int = 300) -> None:
        """Send DTMF digits (for IVR navigation, e.g. "1234#").

        Args:
            digits: String of DTMF digits (0-9, *, #).
            delay_ms: Delay in milliseconds between consecutive digits.
        """
        if self._send_dtmf_fn is not None:
            await self._send_dtmf_fn(digits, delay_ms)
        else:
            logger.warning("send_dtmf() not available for this provider mode")
