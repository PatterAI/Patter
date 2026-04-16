from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger("patter")


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
    to skip the downstream step. Hooks may be sync or async.

    Attributes:
        after_transcribe: Called after STT, before LLM. Return ``None`` to skip turn.
        before_synthesize: Called before TTS, per-sentence in streaming mode.
            Return ``None`` to skip TTS for this sentence.
        after_synthesize: Called after TTS produces an audio chunk.
            Return ``None`` to discard the chunk.
    """

    after_transcribe: Callable | None = None
    before_synthesize: Callable | None = None
    after_synthesize: Callable | None = None


@dataclass(frozen=True)
class Agent:
    """Configuration for a local-mode voice AI agent."""

    system_prompt: str
    voice: str = "alloy"
    model: str = "gpt-4o-mini-realtime-preview"
    language: str = "en"
    first_message: str = ""
    tools: list[dict] | None = None
    provider: str = "openai_realtime"  # "openai_realtime" | "elevenlabs_convai" | "pipeline"
    stt: STTConfig | None = None  # which STT provider to use in pipeline mode
    tts: TTSConfig | None = None  # which TTS provider to use in pipeline mode
    variables: dict | None = None  # Dynamic variables for ``{placeholder}`` substitution in system_prompt
    guardrails: list[Guardrail | dict] | None = None  # List of Guardrail objects or guardrail dicts
    hooks: PipelineHooks | None = None  # Pipeline hooks for pipeline mode


@dataclass(frozen=True)
class CallEvent:
    """Call lifecycle event."""
    call_id: str
    caller: str = ""
    callee: str = ""
    direction: str = ""


@dataclass(frozen=True)
class IncomingMessage:
    text: str
    call_id: str
    caller: str


@dataclass(frozen=True)
class STTConfig:
    provider: str
    api_key: str
    language: str = "en"

    def to_dict(self) -> dict:
        return {"provider": self.provider, "api_key": self.api_key, "language": self.language}


@dataclass(frozen=True)
class TTSConfig:
    provider: str
    api_key: str
    voice: str = "alloy"

    def to_dict(self) -> dict:
        return {"provider": self.provider, "api_key": self.api_key, "voice": self.voice}


@dataclass(frozen=True)
class CostBreakdown:
    """Per-call cost breakdown by segment (USD)."""

    stt: float = 0.0
    tts: float = 0.0
    llm: float = 0.0
    telephony: float = 0.0
    total: float = 0.0


@dataclass(frozen=True)
class LatencyBreakdown:
    """Per-turn latency breakdown (milliseconds)."""

    stt_ms: float = 0.0
    llm_ms: float = 0.0
    tts_ms: float = 0.0
    total_ms: float = 0.0


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
    ):
        self.call_id = call_id
        self.caller = caller
        self.callee = callee
        self.telephony_provider = telephony_provider
        self._transfer_fn = _transfer_fn
        self._hangup_fn = _hangup_fn
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
