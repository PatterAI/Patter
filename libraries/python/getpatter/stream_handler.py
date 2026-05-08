"""Base and concrete StreamHandler classes for provider-mode-specific stream handling.

Each handler encapsulates: provider initialization, audio routing, transcript
handling, conversation history, metrics, guardrails, tool calling, and call
control for a single provider mode (openai_realtime, elevenlabs_convai, pipeline).

The telephony-specific handlers (twilio_handler, telnyx_handler) remain thin
adapters that parse WebSocket messages, transcode audio if needed, and delegate
to the appropriate StreamHandler.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import time
from abc import ABC, abstractmethod
from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

from getpatter.models import HookContext
from getpatter.observability.tracing import (
    SPAN_BARGEIN,
    SPAN_ENDPOINT,
    SPAN_LLM,
    SPAN_STT,
    SPAN_TTS,
    start_span,
)
from getpatter.services.pipeline_hooks import PipelineHookExecutor
from getpatter.services.sentence_chunker import SentenceChunker
from getpatter.telephony.common import (
    _create_stt_from_config,
    _create_tts_from_config,
    _resolve_variables,
    _sanitize_variable_value,
    _validate_e164,
)
from getpatter.utils.log_sanitize import mask_phone_number, sanitize_log_value

logger = logging.getLogger("getpatter")


# Minimum wall-clock duration (seconds) the agent must have been speaking
# before barge-in is allowed to fire. AEC variant (1.0 s) covers the
# filter convergence window. NO_AEC variant (0.25 s) is anti-flicker
# only — used on PSTN where AEC is a no-op so there is no warmup to
# protect, and a long gate just suppresses real-user barge-in.
MIN_AGENT_SPEAKING_S_BEFORE_BARGE_IN_AEC = 1.0
MIN_AGENT_SPEAKING_S_BEFORE_BARGE_IN_NO_AEC = 0.25
# Backwards-compat alias used by tests; matches AEC variant.
MIN_AGENT_SPEAKING_S_BEFORE_BARGE_IN = MIN_AGENT_SPEAKING_S_BEFORE_BARGE_IN_AEC


# ---------------------------------------------------------------------------
# Shared tool definitions injected into every agent
# ---------------------------------------------------------------------------

# Short words / phrases that Whisper (and, less often, Deepgram) routinely
# emit when fed silence or TTS echo on mulaw 8 kHz. Dropping them as turns
# prevents the caller from entering a feedback loop where every silent frame
# triggers a new LLM+TTS turn. Parity with TS ``HALLUCINATIONS``.
_STT_HALLUCINATIONS: frozenset[str] = frozenset(
    {
        "you",
        "thank you",
        "thanks",
        "yeah",
        "yes",
        "no",
        "okay",
        "ok",
        "uh",
        "um",
        "mmm",
        "hmm",
        ".",
        "bye",
        "right",
        "cool",
    }
)


TRANSFER_CALL_TOOL: dict = {
    "name": "transfer_call",
    "description": "Transfer the call to a human agent at the specified phone number",
    "parameters": {
        "type": "object",
        "properties": {
            "number": {
                "type": "string",
                "description": "Phone number to transfer to (E.164 format)",
            }
        },
        "required": ["number"],
    },
}

END_CALL_TOOL: dict = {
    "name": "end_call",
    "description": "End the current phone call. Use when the conversation is complete or the user says goodbye.",
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Reason for ending the call (e.g., 'conversation_complete', 'user_requested', 'no_response')",
            }
        },
    },
}


# ---------------------------------------------------------------------------
# Audio sender protocol — abstracts Twilio vs Telnyx audio output
# ---------------------------------------------------------------------------


class AudioSender(ABC):
    """Protocol for sending audio back to a telephony WebSocket."""

    @abstractmethod
    async def send_audio(self, pcm_audio: bytes) -> None:
        """Send PCM 16 kHz audio to the telephony provider.

        The sender is responsible for any transcoding (e.g. mulaw for Twilio).
        """

    @abstractmethod
    async def send_clear(self) -> None:
        """Clear/stop any currently playing audio."""

    @abstractmethod
    async def send_mark(self, mark_name: str) -> None:
        """Send a playback mark (Twilio-specific; no-op on Telnyx)."""

    def reset_pcm_carry(self) -> None:
        """Drop any buffered odd byte from the PCM16 alignment carry.

        Call at the start/end of a TTS synthesis block so a crash or
        cancellation mid-sentence never bleeds a partial sample into the
        next sentence. Default is a no-op; subclasses that keep a carry
        buffer (e.g. ``TwilioAudioSender``) override this. Matches TS
        parity where ``ttsByteCarry = null`` is reset at every synth
        boundary.
        """
        return None


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def resolve_agent_prompt(agent, custom_params: dict | None = None) -> str:
    """Resolve dynamic variables in the agent's system prompt."""
    resolved = agent.system_prompt
    agent_variables: dict = getattr(agent, "variables", None) or {}
    all_variables = {**agent_variables}
    if custom_params:
        for k, v in custom_params.items():
            all_variables[k] = _sanitize_variable_value(v)
    if all_variables:
        resolved = _resolve_variables(resolved, all_variables)
    return resolved


def apply_call_overrides(agent, overrides: dict):
    """Return a new Agent with per-call config overrides applied."""
    from dataclasses import asdict

    from getpatter.models import (
        Agent as _Agent,
    )
    from getpatter.models import (
        STTConfig as _STTCfg,
    )
    from getpatter.models import (
        TTSConfig as _TTSCfg,
    )

    fields: dict = {}
    for k in (
        "system_prompt",
        "voice",
        "model",
        "language",
        "first_message",
        "provider",
    ):
        if k in overrides:
            fields[k] = overrides[k]
    if "stt_config" in overrides and isinstance(overrides["stt_config"], dict):
        fields["stt"] = _STTCfg(**overrides["stt_config"])
    if "tts_config" in overrides and isinstance(overrides["tts_config"], dict):
        fields["tts"] = _TTSCfg(**overrides["tts_config"])
    if "tools" in overrides:
        fields["tools"] = overrides["tools"]
    if "variables" in overrides:
        fields["variables"] = overrides["variables"]
    if fields:
        base = {k: v for k, v in asdict(agent).items() if k not in fields}
        base.update(fields)
        agent = _Agent(**base)
        logger.debug("Per-call config overrides applied: %s", list(fields.keys()))
    return agent


def create_metrics_accumulator(
    call_id: str,
    provider: str,
    telephony_provider: str,
    agent,
    deepgram_key: str,
    elevenlabs_key: str,
    pricing: dict | None,
    report_only_initial_ttfb: bool = False,
):
    """Create and return a CallMetricsAccumulator for the call."""
    from getpatter.services.metrics import CallMetricsAccumulator

    stt_name = ""
    tts_name = ""
    stt_model = ""
    tts_model = ""
    realtime_model = ""
    if provider == "pipeline":
        # Prefer the explicit ``provider_key`` ClassVar declared by
        # wrapper classes (stable, matches ``pricing.py`` keys); fall
        # back to the legacy ``provider`` instance attribute.
        if agent.stt is not None:
            stt_name = getattr(type(agent.stt), "provider_key", None) or getattr(
                agent.stt, "provider", ""
            )
            # Adapter ``model`` attribute powers per-model rate resolution
            # in pricing.calculate_stt_cost. Empty string → provider default.
            stt_model = str(getattr(agent.stt, "model", "") or "")
        else:
            stt_name = "deepgram" if deepgram_key else ""
        if agent.tts is not None:
            tts_name = getattr(type(agent.tts), "provider_key", None) or getattr(
                agent.tts, "provider", ""
            )
            tts_model = str(getattr(agent.tts, "model", "") or "")
        else:
            tts_name = "elevenlabs" if elevenlabs_key else ""
    elif provider == "openai_realtime":
        stt_name = "openai"
        tts_name = "openai"
        # Realtime collapses STT+LLM+TTS into one model — capture it so the
        # token-based cost calc picks the right per-model rate (e.g. gpt-
        # realtime-2 vs gpt-realtime-mini). Use the agent's declared model
        # when set; fall back to the adapter default.
        realtime_model = str(getattr(agent, "model", "") or "") or "gpt-realtime-mini"
    elif provider == "elevenlabs_convai":
        stt_name = "elevenlabs"
        tts_name = "elevenlabs"
    if provider == "openai_realtime":
        llm_name = "openai"
    elif provider == "elevenlabs_convai":
        llm_name = "elevenlabs"
    else:
        # Resolve the provider key. Prefer the ``provider_key`` ClassVar
        # declared by wrapper classes (stable, matches ``pricing.py``);
        # fall back to the legacy ``__name__`` strip for custom adapters.
        _agent_llm = getattr(agent, "llm", None)
        if _agent_llm is not None:
            _cls = type(_agent_llm)
            _explicit = getattr(_cls, "provider_key", None)
            if _explicit:
                llm_name = _explicit
            else:
                _raw = _cls.__name__.lower()
                for _suffix in ("llmprovider", "provider", "llm"):
                    _raw = _raw.replace(_suffix, "")
                llm_name = _raw or "custom"
        else:
            llm_name = "custom"
    return CallMetricsAccumulator(
        call_id=call_id,
        provider_mode=provider,
        telephony_provider=telephony_provider,
        stt_provider=stt_name,
        tts_provider=tts_name,
        llm_provider=llm_name,
        pricing=pricing,
        report_only_initial_ttfb=report_only_initial_ttfb,
        stt_model=stt_model,
        tts_model=tts_model,
        realtime_model=realtime_model,
    )


def evaluate_guardrails(agent, response_text: str) -> tuple[bool, str]:
    """Evaluate output guardrails against response text.

    Returns (blocked, guard_name). If blocked is True, the response should
    be suppressed.
    """
    guardrails = getattr(agent, "guardrails", None) or []
    for guard in guardrails:
        blocked = False
        blocked_terms = (
            guard.get("blocked_terms")
            if isinstance(guard, dict)
            else getattr(guard, "blocked_terms", None)
        )
        check_fn = (
            guard.get("check")
            if isinstance(guard, dict)
            else getattr(guard, "check", None)
        )
        guard_name = (
            guard.get("name")
            if isinstance(guard, dict)
            else getattr(guard, "name", "unnamed")
        )
        if blocked_terms:
            blocked = any(
                term.lower() in response_text.lower() for term in blocked_terms
            )
        if check_fn and not blocked:
            try:
                blocked = bool(check_fn(response_text))
            except Exception as exc:
                logger.warning("Guardrail '%s' check error: %s", guard_name, exc)
        if blocked:
            logger.warning(
                "Guardrail '%s' triggered on: %.50s", guard_name, response_text
            )
            return True, guard_name
    return False, ""


def get_guardrail_replacement(agent, guard_name: str) -> str:
    """Get the replacement text for a triggered guardrail by name.

    Returns the replacement text from the specific guard that fired,
    falling back to a default message.
    """
    guardrails = getattr(agent, "guardrails", None) or []
    for guard in guardrails:
        name = (
            guard.get("name")
            if isinstance(guard, dict)
            else getattr(guard, "name", "unnamed")
        )
        if name == guard_name:
            r = (
                guard.get("replacement")
                if isinstance(guard, dict)
                else getattr(guard, "replacement", None)
            )
            if r:
                return r
    return "I'm sorry, I can't respond to that."


# ---------------------------------------------------------------------------
# Base StreamHandler
# ---------------------------------------------------------------------------


class StreamHandler(ABC):
    """Base class for provider-mode-specific stream handling.

    Subclasses implement the core logic for OpenAI Realtime, ElevenLabs ConvAI,
    or Pipeline mode. The telephony handler creates the appropriate subclass
    and delegates audio/lifecycle events.
    """

    def __init__(
        self,
        agent,
        audio_sender: AudioSender,
        call_id: str,
        caller: str,
        callee: str,
        resolved_prompt: str,
        metrics,
        *,
        on_transcript=None,
        on_message=None,
        on_metrics=None,
        conversation_history: deque | None = None,
        transcript_entries: deque | None = None,
        speech_events: Any = None,
    ) -> None:
        self.agent = agent
        self.audio_sender = audio_sender
        self.call_id = call_id
        self.caller = caller
        self.callee = callee
        self.resolved_prompt = resolved_prompt
        self.metrics = metrics
        self.on_transcript = on_transcript
        self.on_message = on_message
        self.on_metrics = on_metrics
        self.conversation_history: deque = conversation_history or deque(maxlen=200)
        self.transcript_entries: deque = transcript_entries or deque(maxlen=200)
        # Optional `SpeechEvents` dispatcher. When set, the handler emits
        # turn-taking edges (VAD start/stop, EOU commit, agent first/last
        # wire chunk) as the call progresses. None == prior behaviour.
        self.speech_events = speech_events
        # Tracks the wall-clock when the user's current speaking segment
        # began, so `fire_user_speech_ended` can include `speech_duration_ms`.
        self._user_speech_start_ms: float | None = None
        # Tracks the wall-clock when the agent's current turn began
        # speaking on the wire, for `fire_agent_speech_ended.speech_duration_ms`.
        self._agent_turn_start_ms: float | None = None
        self._background_task: asyncio.Task | None = None
        # MCP server connection manager — populated lazily in
        # ``_init_mcp_tools`` when the agent declares ``mcp_servers``.
        # Closed in ``cleanup``/``fire_call_end`` to free open MCP
        # WebSocket / HTTP connections. Parity with TS field.
        self._mcp_manager: Any = None

        # Create one EventBus per handler instance and wire it to metrics.
        from getpatter.observability.event_bus import EventBus as _EventBus

        self._event_bus: _EventBus = _EventBus()
        if self.metrics is not None and hasattr(self.metrics, "attach_event_bus"):
            self.metrics.attach_event_bus(self._event_bus)

    async def _init_mcp_tools(self) -> None:
        """Connect to every configured MCP server, discover their tools
        via ``tools/list``, and merge them into ``agent.tools`` before
        the adapter is built. The synthetic handlers dispatch back
        through the MCP client so ``ToolExecutor`` can invoke them like
        any other handler-tool. No-op when ``agent.mcp_servers`` is
        empty or the optional ``mcp`` package is not installed."""
        servers = getattr(self.agent, "mcp_servers", None)
        if not servers:
            return
        from getpatter.tools.mcp_client import MCPManager

        manager = MCPManager(servers)
        try:
            discovered = await manager.connect()
        except Exception as exc:
            logger.error("MCP connect failed (continuing without MCP tools): %s", exc)
            return
        if not discovered:
            return
        existing = list(self.agent.tools or [])
        MCPManager.assert_no_conflicts(existing, discovered)
        # ``Agent`` is a frozen dataclass — replace it with a copy that
        # has the merged tool list so the adapter and ToolExecutor see
        # the discovered tools alongside user-defined ones.
        import dataclasses

        self.agent = dataclasses.replace(self.agent, tools=existing + discovered)
        self._mcp_manager = manager
        logger.info("MCP: merged %d tool(s) into agent", len(discovered))

    async def _close_mcp(self) -> None:
        """Close MCP connections opened by :meth:`_init_mcp_tools`."""
        manager = self._mcp_manager
        self._mcp_manager = None
        if manager is None:
            return
        try:
            await manager.close()
        except Exception as exc:
            logger.debug("MCP close error (ignored): %s", exc)

    def add_observer(self, fn) -> None:
        """Register *fn* as an observer for all ``metrics_collected`` events.

        Convenience wrapper around :meth:`EventBus.on` that exposes a stable
        public API for external monitoring tools::

            handler.add_observer(lambda payload: print(payload))

        Returns ``None``; to unsubscribe, call :meth:`EventBus.on` directly.

        Args:
            fn: Callable that accepts a single payload dict. May be sync or
                async (async callables are scheduled via asyncio.create_task).
        """
        self._event_bus.on("metrics_collected", fn)

    # ------------------------------------------------------------------
    # Speech-event helpers — no-op when no SpeechEvents dispatcher is set.
    # ------------------------------------------------------------------

    async def _emit_user_speech_started(self) -> None:
        if self.speech_events is None:
            return
        self._user_speech_start_ms = time.time() * 1000
        await self.speech_events.fire_user_speech_started()

    async def _emit_user_speech_ended(self) -> None:
        if self.speech_events is None:
            return
        now_ms = time.time() * 1000
        duration_ms = (
            int(now_ms - self._user_speech_start_ms)
            if self._user_speech_start_ms is not None
            else 0
        )
        self._user_speech_start_ms = None
        await self.speech_events.fire_user_speech_ended(
            speech_duration_ms=max(0, duration_ms)
        )

    async def _emit_user_speech_eos(
        self, *, trigger: str, transcript_so_far: str | None = None
    ) -> None:
        if self.speech_events is None:
            return
        await self.speech_events.fire_user_speech_eos(
            trigger=trigger, transcript_so_far=transcript_so_far
        )

    async def _emit_agent_speech_started(self, *, engine: str | None = None) -> None:
        if self.speech_events is None:
            return
        self._agent_turn_start_ms = time.time() * 1000
        tts_provider = self._infer_tts_provider()
        await self.speech_events.fire_agent_speech_started(
            tts_provider=tts_provider, engine=engine
        )

    async def _emit_agent_speech_ended(self, *, interrupted: bool = False) -> None:
        if self.speech_events is None:
            return
        now_ms = time.time() * 1000
        duration_ms = (
            int(now_ms - self._agent_turn_start_ms)
            if self._agent_turn_start_ms is not None
            else 0
        )
        self._agent_turn_start_ms = None
        await self.speech_events.fire_agent_speech_ended(
            speech_duration_ms=max(0, duration_ms), interrupted=interrupted
        )

    async def _emit_llm_first_token(
        self, *, llm_provider: str, model: str | None = None
    ) -> None:
        """Fire the per-turn TTFT marker. Idempotent within a turn —
        :class:`SpeechEvents` guards on ``_first_token_for_turn``.
        """
        if self.speech_events is None:
            return
        await self.speech_events.fire_llm_first_token(
            llm_provider=llm_provider, model=model or ""
        )

    async def _emit_audio_out(self, *, tts_provider: str | None = None) -> None:
        """Fire the per-turn first-TTS-chunk marker. Idempotent within a
        turn — :class:`SpeechEvents` guards on ``_first_audio_for_turn``.
        ``tts_provider`` defaults to the inferred TTS class name (Pipeline
        mode) or the engine name (Realtime / ConvAI).
        """
        if self.speech_events is None:
            return
        provider = tts_provider or self._infer_tts_provider() or "unknown"
        await self.speech_events.fire_audio_out(tts_provider=provider)

    def _infer_tts_provider(self) -> str | None:
        """Best-effort TTS provider name for event payloads. Returns None
        when the handler has no TTS (Realtime / ConvAI engines) or the
        provider can't be classified."""
        tts = getattr(self, "tts_provider", None) or getattr(self, "_tts", None)
        if tts is None:
            return None
        cls_name = type(tts).__name__.lower()
        # Heuristic: provider classes are named like ``ElevenLabsTTS``,
        # ``OpenAITTS``, ``CartesiaTTS`` etc.
        for known in (
            "elevenlabs",
            "openai",
            "cartesia",
            "rime",
            "lmnt",
            "inworld",
            "telnyx",
        ):
            if known in cls_name:
                return known
        return cls_name.replace("tts", "") or None

    def _infer_llm_provider(self) -> str:
        """Best-effort LLM provider name for event payloads. Returns the
        agent's configured LLM provider class name lower-cased, or
        ``"openai"`` when only the OpenAI key path is in use."""
        llm = getattr(self.agent, "llm", None)
        if llm is None:
            return "openai"
        cls_name = type(llm).__name__.lower()
        for known in (
            "anthropic",
            "cerebras",
            "groq",
            "google",
            "gemini",
            "openai",
            "azure",
            "mistral",
            "deepseek",
        ):
            if known in cls_name:
                return known
        return cls_name.replace("llmprovider", "").replace("llm", "") or "custom"

    @abstractmethod
    async def start(self) -> None:
        """Initialize provider connections and start background tasks."""

    @abstractmethod
    async def on_audio_received(self, audio_bytes: bytes) -> None:
        """Handle incoming audio from the telephony provider (already decoded)."""

    async def on_dtmf(self, digit: str) -> None:
        """Handle DTMF keypress. Override in subclasses that support it."""

    async def on_mark(self, mark_name: str) -> None:
        """Handle playback mark confirmation. Override if needed."""

    @abstractmethod
    async def cleanup(self) -> None:
        """Close provider connections and cancel background tasks."""

    async def _emit_turn_metrics(self, turn, *, call_id: str | None = None) -> None:
        """Emit a completed turn to the user-supplied on_metrics callback.

        All emit sites share the same payload shape
        (``{call_id, turn, cost_so_far}``). Callers remain responsible for
        appending transcript entries / storing the turn; only the user-facing
        callback is centralised here for parity with TS ``emitTurnMetrics``.
        """
        if not self.on_metrics or turn is None or self.metrics is None:
            return
        await self.on_metrics(
            {
                "call_id": call_id if call_id is not None else self.call_id,
                "turn": turn,
                "cost_so_far": self.metrics.get_cost_so_far(),
                # Fix 5: expose LLM TTFT separately from full-generation llm_ms.
                "llm_ttft_ms": self.metrics.last_turn_llm_ttft_ms,
            }
        )


# ---------------------------------------------------------------------------
# OpenAI Realtime StreamHandler
# ---------------------------------------------------------------------------


#: Hard cap on how long the Realtime path waits for the user transcript to
#: arrive before flushing the buffered assistant turn alone. 3 s covers
#: OpenAI Whisper's typical 200-800 ms post-response delay with substantial
#: headroom; beyond this we accept the order will look "assistant-only"
#: rather than block the dashboard transcript display indefinitely.
_REALTIME_USER_TRANSCRIPT_WAIT_S = 3.0


class OpenAIRealtimeStreamHandler(StreamHandler):
    """Handles the openai_realtime provider mode."""

    def __init__(
        self,
        agent,
        audio_sender: AudioSender,
        call_id: str,
        caller: str,
        callee: str,
        resolved_prompt: str,
        metrics,
        *,
        openai_key: str,
        transfer_fn=None,
        hangup_fn=None,
        on_transcript=None,
        on_metrics=None,
        conversation_history: deque | None = None,
        transcript_entries: deque | None = None,
        audio_format: str = "pcm16",
        input_transcode: str | None = None,
        speech_events=None,
    ) -> None:
        super().__init__(
            agent=agent,
            audio_sender=audio_sender,
            call_id=call_id,
            caller=caller,
            callee=callee,
            resolved_prompt=resolved_prompt,
            metrics=metrics,
            on_transcript=on_transcript,
            on_metrics=on_metrics,
            conversation_history=conversation_history,
            transcript_entries=transcript_entries,
            speech_events=speech_events,
        )
        self._openai_key = openai_key
        self._transfer_fn = transfer_fn
        self._hangup_fn = hangup_fn
        self._audio_format = audio_format
        # OpenAI Realtime API uses a single codec for both input and output
        # (``audio_format`` becomes both ``input_audio_format`` and
        # ``output_audio_format`` in the session). When the telephony leg
        # delivers a different codec than what we want to send back (e.g.
        # Telnyx inbound = PCM16 16 kHz, outbound = PCMU 8 kHz), set
        # ``input_transcode`` to convert inbound bytes to match ``audio_format``
        # before forwarding to OpenAI.
        #
        # Supported values:
        #   ``"pcm16_16k_to_g711_ulaw"`` — Telnyx inbound PCM16 16 kHz →
        #       mulaw 8 kHz (matches ``audio_format="g711_ulaw"``).
        self._input_transcode = input_transcode
        self._adapter = None
        # Per-handler StatefulResampler for pcm16_16k_to_g711_ulaw transcoding.
        self._resampler_16k_to_8k = None
        # Realtime turn ordering buffer. OpenAI Realtime emits the
        # user-transcript-completion event AFTER response_done, because
        # Whisper transcription runs in parallel with — and slower than —
        # the model response. Without this buffer the conversation_history
        # push order is [assistant, user, ...] which renders out-of-order
        # in the dashboard. See TS parity in stream-handler.ts.
        self._user_transcript_pending = False
        self._pending_assistant_turn: str | None = None
        self._pending_assistant_timer: asyncio.Task | None = None

    async def _flush_assistant_turn(self, text: str) -> None:
        """Push an assistant turn into history, fire ``on_transcript``, and
        emit turn-complete metrics. Shared between the immediate path (no
        user transcript pending) and the buffered path (flushed after the
        user transcript arrives or the fallback timer fires)."""
        self.conversation_history.append(
            {"role": "assistant", "text": text, "timestamp": time.time()}
        )
        self.transcript_entries.append({"role": "assistant", "text": text})
        if self.on_transcript:
            await self.on_transcript(
                {
                    "role": "assistant",
                    "text": text,
                    "call_id": self.call_id,
                    "history": list(self.conversation_history),
                }
            )
        if self.metrics is not None:
            turn = self.metrics.record_turn_complete(text)
            await self._emit_turn_metrics(turn)

    async def _assistant_buffer_timeout(self) -> None:
        """Fallback flush: if the user transcript never arrives, surface
        the assistant turn alone after the wait window."""
        try:
            await asyncio.sleep(_REALTIME_USER_TRANSCRIPT_WAIT_S)
        except asyncio.CancelledError:
            return
        buffered = self._pending_assistant_turn
        self._pending_assistant_turn = None
        self._pending_assistant_timer = None
        self._user_transcript_pending = False
        if buffered is not None:
            try:
                await self._flush_assistant_turn(buffered)
            except Exception:
                logger.exception("Assistant buffer flush (timeout) failed")

    def _schedule_reassurance(
        self, tool_def: dict, tool_name: str
    ) -> asyncio.Task | None:
        """Schedule a reassurance filler message if the tool has one
        configured. Bridges the silence when a slow tool call would
        otherwise leave the caller hanging. Returns the task so the
        caller can cancel it on tool completion. Parity with TS
        ``handleFunctionCall`` reassurance scheduling."""
        config = tool_def.get("reassurance")
        if not config:
            return None
        if isinstance(config, str):
            message = config
            after_ms = 1500
        elif isinstance(config, dict):
            message = config.get("message", "")
            after_ms = int(config.get("after_ms", 1500))
        else:
            return None
        if not message:
            return None

        adapter = self._adapter
        if adapter is None or not hasattr(adapter, "send_text"):
            return None

        async def _fire() -> None:
            try:
                await asyncio.sleep(after_ms / 1000.0)
                await adapter.send_text(message)
            except asyncio.CancelledError:
                # Tool returned before the grace window — nothing to do.
                raise
            except Exception as exc:
                logger.warning(
                    "Reassurance message failed for tool '%s': %s", tool_name, exc
                )

        return asyncio.create_task(_fire())

    async def _emit_tool_event(
        self,
        name: str,
        args: dict | None,
        result: str | None,
    ) -> None:
        """Surface a tool invocation into the transcript timeline. Pushes
        ``role="tool"`` into history (for the dashboard) and fires
        ``on_transcript`` so the host application can log / persist /
        render it. Result is truncated for log readability — the full
        payload is in history."""
        args_text = json.dumps(args or {})
        if result is None:
            text = f"{name}({args_text})"
        else:
            displayed = result if len(result) <= 200 else result[:200] + "…"
            text = f"{name}({args_text}) → {displayed}"
        self.conversation_history.append(
            {"role": "tool", "text": text, "timestamp": time.time()}
        )
        self.transcript_entries.append({"role": "tool", "text": text})
        if self.on_transcript:
            await self.on_transcript(
                {
                    "role": "tool",
                    "text": text,
                    "call_id": self.call_id,
                    "tool_name": name,
                    "tool_args": args or {},
                    "tool_result": result,
                }
            )

    async def start(self) -> None:
        """Connect to OpenAI Realtime, register tools, and begin event forwarding."""
        from getpatter.providers.openai_realtime import (
            OpenAIRealtimeAdapter,  # type: ignore[import]
        )

        # Resolve MCP servers BEFORE the adapter is built so the
        # discovered tools are visible in the first ``session.update``.
        # Failures are logged but not fatal — a dead MCP server should
        # not kill the entire call. Parity with TS ``initMcpTools``.
        await self._init_mcp_tools()

        agent_tools: list[dict] = []
        for t in self.agent.tools or []:
            entry: dict = {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("parameters", {}),
            }
            # Propagate strict-mode opt-in to the OpenAI session.update
            # wire format. Schema is already validated at agent() build
            # time so we can pass it through without re-checking.
            if t.get("strict") is True:
                entry["strict"] = True
            agent_tools.append(entry)
        openai_tools: list[dict] = agent_tools + [TRANSFER_CALL_TOOL, END_CALL_TOOL]

        # Forward optional engine-level Realtime knobs (carried on the Agent
        # by ``Patter._unpack_engine``) only when set, so the adapter's own
        # defaults remain authoritative for users that don't pass them.
        adapter_kwargs: dict = {
            "api_key": self._openai_key,
            "model": self.agent.model,
            "voice": self.agent.voice,
            "instructions": self.resolved_prompt,
            "language": self.agent.language,
            "tools": openai_tools,
            "audio_format": self._audio_format,
        }
        reasoning_effort = getattr(self.agent, "openai_realtime_reasoning_effort", None)
        if reasoning_effort is not None:
            adapter_kwargs["reasoning_effort"] = reasoning_effort
        transcription_model = getattr(
            self.agent, "openai_realtime_input_audio_transcription_model", None
        )
        if transcription_model is not None:
            adapter_kwargs["input_audio_transcription_model"] = transcription_model
        self._adapter = OpenAIRealtimeAdapter(**adapter_kwargs)
        await self._adapter.connect()
        logger.debug("OpenAI Realtime connected")

        if self.agent.first_message:
            # Start measuring latency for the firstMessage turn (sendText →
            # first audio byte). Parity with TS handler.
            if self.metrics is not None:
                self.metrics.start_turn()
            # Use ``send_first_message`` (role=assistant) so the AI treats
            # ``first_message`` as its OWN opening line, not a user prompt
            # to respond to. Older adapters that don't expose the new method
            # fall back to ``send_text``.
            sender = getattr(
                self._adapter, "send_first_message", self._adapter.send_text
            )
            await sender(self.agent.first_message)

        self._background_task = asyncio.create_task(self._forward_events())

    async def _forward_events(self) -> None:
        from getpatter.tools.tool_executor import ToolExecutor  # type: ignore[import]

        tool_executor = ToolExecutor()
        # Arm first-byte capture so that the firstMessage turn (started in
        # start()) gets its tts_ms / total_ms recorded on the first audio
        # chunk. Parity with TS ``responseAudioStarted=false`` class field.
        waiting_first_audio = True
        current_agent_text = ""
        try:
            async for ev_type, ev_data in self._adapter.receive_events():
                if ev_type == "audio":
                    # Fallback: if audio arrives before speech_stopped (which
                    # can happen when JS/async event loop reorders WS frames
                    # under load, or with server VAD disabled) start the turn
                    # now so latency is still measured. Parity with TS.
                    if self.metrics is not None and not self.metrics.turn_active:
                        self.metrics.start_turn()
                    if waiting_first_audio:
                        if self.metrics is not None:
                            self.metrics.record_tts_first_byte()
                        # Speech-event: first wire-time chunk of this agent turn.
                        await self._emit_agent_speech_started(engine="openai_realtime")
                        # Speech-event: first TTS audio chunk of this turn.
                        # In Realtime mode the LLM and TTS are the same model
                        # (audio-out IS the model output), so the same edge
                        # marks both ``llm_first_token`` and ``tts_first_audio``
                        # for the SDK callback consumers. The dispatcher
                        # idempotency guards stop double-firing within a turn.
                        await self._emit_audio_out(tts_provider="openai_realtime")
                        waiting_first_audio = False
                    await self.audio_sender.send_audio(ev_data)
                    await self.audio_sender.send_mark(f"audio_{id(ev_data)}")

                elif ev_type == "speech_stopped":
                    # OpenAI server-side VAD detected end-of-user-speech.
                    # This is the earliest reliable moment to start measuring
                    # turn latency in Realtime mode — transcript_input arrives
                    # noticeably later and understates end-to-end latency.
                    if self.metrics is not None and not self.metrics.turn_active:
                        self.metrics.start_turn()
                    waiting_first_audio = True
                    current_agent_text = ""
                    # Mark a user transcript is expected so response_done
                    # waits for it before pushing the assistant turn.
                    self._user_transcript_pending = True
                    # Speech-event: raw VAD trailing edge. EOU is committed
                    # later on `transcript_input` (Realtime emits it after
                    # input_audio_buffer.committed).
                    await self._emit_user_speech_ended()

                elif ev_type == "transcript_input":
                    logger.debug("User: %s", sanitize_log_value(ev_data))
                    if self.metrics is not None:
                        # Fallback: start turn here if speech_stopped was missed
                        # (server VAD disabled or custom config).
                        if not self.metrics.turn_active:
                            self.metrics.start_turn()
                        self.metrics.record_stt_complete(ev_data)
                    waiting_first_audio = True
                    current_agent_text = ""
                    # Speech-event: end-of-utterance committed (Realtime mode
                    # emits this on `input_audio_buffer.committed`, which is
                    # the canonical "user finished" signal).
                    await self._emit_user_speech_eos(
                        trigger="vad_silence", transcript_so_far=ev_data
                    )

                    self.conversation_history.append(
                        {"role": "user", "text": ev_data, "timestamp": time.time()}
                    )
                    self.transcript_entries.append({"role": "user", "text": ev_data})
                    if self.on_transcript:
                        await self.on_transcript(
                            {
                                "role": "user",
                                "text": ev_data,
                                "call_id": self.call_id,
                                "history": list(self.conversation_history),
                            }
                        )
                    # User transcript landed — flush any assistant turn
                    # that was buffered waiting for it.
                    self._user_transcript_pending = False
                    if self._pending_assistant_turn is not None:
                        buffered = self._pending_assistant_turn
                        self._pending_assistant_turn = None
                        if self._pending_assistant_timer is not None:
                            self._pending_assistant_timer.cancel()
                            self._pending_assistant_timer = None
                        await self._flush_assistant_turn(buffered)

                elif ev_type == "transcript_output":
                    if ev_data:
                        response_text: str = ev_data
                        # Speech-event: first LLM token (TTFT) for this turn.
                        # Idempotent — dispatcher guards on
                        # ``_first_token_for_turn``.
                        await self._emit_llm_first_token(
                            llm_provider="openai_realtime",
                            model=self.agent.model,
                        )
                        blocked, guard_name = evaluate_guardrails(
                            self.agent, response_text
                        )
                        if blocked:
                            await self._adapter.cancel_response()
                            replacement = get_guardrail_replacement(
                                self.agent, guard_name
                            )
                            await self._adapter.send_text(replacement)
                            current_agent_text = ""
                        else:
                            # Accumulate deltas — push single entry on response_done
                            current_agent_text += response_text

                elif ev_type == "speech_started":
                    await self.audio_sender.send_clear()
                    await self._adapter.cancel_response()
                    if self.metrics is not None:
                        self.metrics.record_turn_interrupted()
                    # Speech-event: user started speaking. If the agent was
                    # mid-turn this is a barge-in — close out the agent turn
                    # as interrupted before flagging the new user-speech edge,
                    # so consumers see ``agent_ended(interrupted=true)`` →
                    # ``user_started`` in causal order.
                    if not waiting_first_audio:
                        await self._emit_agent_speech_ended(interrupted=True)
                    await self._emit_user_speech_started()
                    waiting_first_audio = False
                    current_agent_text = ""
                    # Barge-in invalidates any buffered assistant turn —
                    # the user interrupted before the response was
                    # committed; do not surface it as if completed.
                    self._pending_assistant_turn = None
                    if self._pending_assistant_timer is not None:
                        self._pending_assistant_timer.cancel()
                        self._pending_assistant_timer = None
                    self._user_transcript_pending = False

                elif ev_type == "response_done":
                    if self.metrics is not None and isinstance(ev_data, dict):
                        usage = ev_data.get("usage", {})
                        if usage:
                            # ``response.done`` carries the model used for
                            # this turn (e.g. ``gpt-realtime-2``); pass it
                            # so the cost calc auto-resolves the per-model
                            # rate. Falls back to ``self.realtime_model`` set
                            # at call start when absent.
                            self.metrics.record_realtime_usage(
                                usage, model=ev_data.get("model")
                            )
                    response_was_cancelled = (
                        not current_agent_text
                        and self.metrics is not None
                        and self.metrics.turn_active
                    )
                    if current_agent_text:
                        text_to_flush = current_agent_text
                        current_agent_text = ""
                        if self._user_transcript_pending:
                            # Buffer until the user transcript arrives so
                            # the rendered order is [user, assistant, ...]
                            # rather than [assistant, user, ...].
                            self._pending_assistant_turn = text_to_flush
                            if self._pending_assistant_timer is not None:
                                self._pending_assistant_timer.cancel()
                            self._pending_assistant_timer = asyncio.create_task(
                                self._assistant_buffer_timeout()
                            )
                        else:
                            await self._flush_assistant_turn(text_to_flush)
                    elif self.metrics is not None and self.metrics.turn_active:
                        # response_done without agent text = cancelled / empty
                        # response. Close the active turn as interrupted so the
                        # next speech_stopped can start a fresh turn cleanly.
                        # Parity with TS handleAdapterEvent response_done path.
                        self.metrics.record_turn_interrupted()
                    # Speech-event: agent finished its turn. ``interrupted``
                    # tracks whether the response was cut by barge-in (no
                    # text emitted) versus a clean completion. We only fire
                    # when an agent turn was actually in flight (start_ms is
                    # set), to avoid spurious events on engine warmup.
                    if self._agent_turn_start_ms is not None:
                        await self._emit_agent_speech_ended(
                            interrupted=response_was_cancelled
                        )
                    waiting_first_audio = True

                elif ev_type == "function_call":
                    func_data = ev_data
                    if func_data["name"] == "transfer_call":
                        raw_args = func_data.get("arguments", "{}")
                        args = (
                            json.loads(raw_args)
                            if isinstance(raw_args, str)
                            else raw_args
                        )
                        transfer_number = args.get("number", "")
                        if not _validate_e164(transfer_number):
                            logger.warning(
                                "transfer_call rejected: invalid number %s",
                                mask_phone_number(transfer_number),
                            )
                            rejection = json.dumps(
                                {
                                    "error": "Invalid phone number format",
                                    "status": "rejected",
                                }
                            )
                            await self._adapter.send_function_result(
                                func_data["call_id"], rejection
                            )
                            await self._emit_tool_event(
                                "transfer_call", args, rejection
                            )
                            continue
                        logger.debug(
                            "Transferring call to %s",
                            mask_phone_number(transfer_number),
                        )
                        result = json.dumps(
                            {"status": "transferring", "to": transfer_number}
                        )
                        await self._adapter.send_function_result(
                            func_data["call_id"], result
                        )
                        await self._emit_tool_event("transfer_call", args, result)
                        if self._transfer_fn:
                            await self._transfer_fn(transfer_number)
                        if self.on_transcript:
                            await self.on_transcript(
                                {
                                    "role": "system",
                                    "text": f"Call transferred to {transfer_number}",
                                    "call_id": self.call_id,
                                }
                            )
                        return

                    elif func_data["name"] == "end_call":
                        raw_args = func_data.get("arguments", "{}")
                        args = (
                            json.loads(raw_args)
                            if isinstance(raw_args, str)
                            else raw_args
                        )
                        reason = args.get("reason", "conversation_complete")
                        logger.debug("Ending call: %s", reason)
                        result = json.dumps({"status": "ending", "reason": reason})
                        await self._adapter.send_function_result(
                            func_data["call_id"], result
                        )
                        await self._emit_tool_event("end_call", args, result)
                        if self._hangup_fn:
                            await self._hangup_fn()
                        if self.on_transcript:
                            await self.on_transcript(
                                {
                                    "role": "system",
                                    "text": f"Call ended: {reason}",
                                    "call_id": self.call_id,
                                }
                            )
                        return

                    else:
                        tool_def = next(
                            (
                                t
                                for t in (self.agent.tools or [])
                                if t["name"] == func_data["name"]
                            ),
                            None,
                        )
                        if tool_def and (
                            tool_def.get("webhook_url") or tool_def.get("handler")
                        ):
                            args = func_data.get("arguments", "{}")
                            if isinstance(args, str):
                                args = json.loads(args)
                            # Surface the invocation BEFORE execution so the
                            # dashboard timeline shows it at the right point
                            # even if the handler throws or hangs.
                            await self._emit_tool_event(func_data["name"], args, None)
                            # Schedule reassurance filler if configured —
                            # bridges silence on slow tool calls. Cleared
                            # in finally below. Parity with TS handler.
                            reassurance_task = self._schedule_reassurance(
                                tool_def, func_data["name"]
                            )
                            # Progress sink: when the handler is an async
                            # generator that yields ``{"progress": "..."}``,
                            # forward each progress message via the Realtime
                            # adapter so the agent speaks the update inline.
                            adapter_for_progress = self._adapter

                            async def _on_progress(text: str) -> None:
                                if hasattr(adapter_for_progress, "send_text"):
                                    try:
                                        await adapter_for_progress.send_text(text)
                                    except Exception as exc:
                                        logger.warning(
                                            "Tool progress message failed: %s",
                                            exc,
                                        )

                            try:
                                result = await tool_executor.execute(
                                    tool_name=func_data["name"],
                                    arguments=args,
                                    call_context={
                                        "call_id": self.call_id,
                                        "caller": self.caller,
                                        "callee": self.callee,
                                    },
                                    webhook_url=tool_def.get("webhook_url", ""),
                                    handler=tool_def.get("handler"),
                                    on_progress=_on_progress,
                                )
                            finally:
                                if reassurance_task is not None:
                                    reassurance_task.cancel()
                            await self._adapter.send_function_result(
                                func_data["call_id"], result
                            )
                            # Emit follow-up event with result so timeline
                            # shows full call/return semantics.
                            await self._emit_tool_event(func_data["name"], args, result)
        except Exception as exc:
            logger.exception("OpenAI Realtime forward error: %s", exc)

    async def on_audio_received(self, audio_bytes: bytes) -> None:
        """Forward decoded telephony audio to the OpenAI Realtime session (transcoding if needed)."""
        if self._adapter is None:
            return
        if self._input_transcode == "pcm16_16k_to_g711_ulaw":
            from getpatter.audio.transcoding import pcm16_to_mulaw

            # Use per-handler StatefulResampler to preserve ratecv filter state
            # across chunks and prevent boundary artefacts.
            if self._resampler_16k_to_8k is None:
                from getpatter.audio.transcoding import create_resampler_16k_to_8k

                self._resampler_16k_to_8k = create_resampler_16k_to_8k()
            audio_bytes = pcm16_to_mulaw(self._resampler_16k_to_8k.process(audio_bytes))
        await self._adapter.send_audio(audio_bytes)

    async def on_dtmf(self, digit: str) -> None:
        """Forward a DTMF keypress to the model as a synthetic user message."""
        if self._adapter is not None:
            await self._adapter.send_text(
                f"The user pressed key {digit} on their phone keypad."
            )

    async def cleanup(self) -> None:
        """Cancel the event-forward task and close the OpenAI Realtime adapter."""
        if self._background_task:
            self._background_task.cancel()
            try:
                await self._background_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._adapter:
            await self._adapter.close()
        # Close MCP server connections. Best effort: a flaky MCP server
        # must not derail call-end teardown.
        await self._close_mcp()
        # Flush and discard the resampler tail on cleanup.
        if self._resampler_16k_to_8k is not None:
            self._resampler_16k_to_8k.flush()
            self._resampler_16k_to_8k = None


# ---------------------------------------------------------------------------
# ElevenLabs ConvAI StreamHandler
# ---------------------------------------------------------------------------


class ElevenLabsConvAIStreamHandler(StreamHandler):
    """Handles the elevenlabs_convai provider mode."""

    def __init__(
        self,
        agent,
        audio_sender: AudioSender,
        call_id: str,
        caller: str,
        callee: str,
        resolved_prompt: str,
        metrics,
        *,
        elevenlabs_key: str,
        for_twilio: bool = False,
        on_transcript=None,
        on_metrics=None,
        conversation_history: deque | None = None,
        transcript_entries: deque | None = None,
        output_audio_format: str | None = None,
        input_audio_format: str | None = None,
    ) -> None:
        super().__init__(
            agent=agent,
            audio_sender=audio_sender,
            call_id=call_id,
            caller=caller,
            callee=callee,
            resolved_prompt=resolved_prompt,
            metrics=metrics,
            on_transcript=on_transcript,
            on_metrics=on_metrics,
            conversation_history=conversation_history,
            transcript_entries=transcript_entries,
        )
        self._elevenlabs_key = elevenlabs_key
        self._for_twilio = for_twilio
        # Caller-supplied codec overrides win over agent.elevenlabs_convai
        # config (resolved at start() time so the integration test that
        # mocks the adapter doesn't crash on a missing config dict).
        self._output_audio_format_override = output_audio_format
        self._input_audio_format_override = input_audio_format
        # When True (set in start() once we know the negotiated formats),
        # forward inbound caller audio as raw μ-law 8 kHz and skip the
        # outbound PCM16 → μ-law transcode in the audio sender. Mirrors
        # OpenAIRealtimeStreamHandler's ``audio_format='g711_ulaw'`` path.
        self._native_mulaw_8k = False
        self._adapter = None
        # Per-handler StatefulResampler for Twilio mulaw 8 kHz -> PCM16 16 kHz.
        # Only created when we actually need to resample (i.e. ConvAI
        # negotiated PCM16 16 kHz, not native μ-law).
        self._resampler_8k_to_16k = None

    async def start(self) -> None:
        """Connect to the ElevenLabs ConvAI agent and begin event forwarding."""
        from getpatter.providers.elevenlabs_convai import (
            ElevenLabsConvAIAdapter,  # type: ignore[import]
        )

        voice = (
            self.agent.voice if self.agent.voice != "alloy" else "EXAVITQu4vr4xnSDxMaL"
        )
        agent_id = ""
        el_config = getattr(self.agent, "elevenlabs_convai", None) or {}
        if isinstance(el_config, dict):
            agent_id = el_config.get("agent_id", "")

        if not agent_id:
            raise ValueError(
                "ElevenLabs ConvAI requires agent.elevenlabs_convai={'agent_id': '...'}. "
                "Create an agent in the ElevenLabs Conversational AI dashboard "
                "and pass its id."
            )

        # Resolve negotiated audio formats. Precedence (highest to lowest):
        #   1. Explicit handler kwargs (output_audio_format / input_audio_format)
        #   2. agent.elevenlabs_convai dict ("output_audio_format", "input_audio_format")
        #   3. None — let ConvAI pick its server default (PCM16 16 kHz)
        cfg_output = (
            el_config.get("output_audio_format")
            if isinstance(el_config, dict)
            else None
        )
        cfg_input = (
            el_config.get("input_audio_format") if isinstance(el_config, dict) else None
        )
        output_audio_format = self._output_audio_format_override or cfg_output
        input_audio_format = self._input_audio_format_override or cfg_input

        self._adapter = ElevenLabsConvAIAdapter(
            api_key=self._elevenlabs_key,
            agent_id=agent_id,
            voice_id=voice,
            language=self.agent.language,
            first_message=self.agent.first_message,
            output_audio_format=output_audio_format,
            input_audio_format=input_audio_format,
        )

        # Detect the μ-law 8 kHz fast-path. Both directions must be
        # ulaw_8000 — mixing PCM16 with μ-law would force one transcode
        # back, defeating the optimization.
        self._native_mulaw_8k = (
            output_audio_format == "ulaw_8000" and input_audio_format == "ulaw_8000"
        )
        if self._native_mulaw_8k:
            # Flip the audio sender into pass-through mode. Mirrors how
            # OpenAIRealtimeStreamHandler relies on the bridge constructing
            # the sender with ``input_is_mulaw_8k=True``. We can't change
            # that wiring from inside the handler, so we mutate the flag
            # in place — the AudioSender's ``send_audio`` checks it on
            # every chunk, so flipping it before the first agent audio
            # arrives is safe.
            if hasattr(self.audio_sender, "_input_is_mulaw_8k"):
                self.audio_sender._input_is_mulaw_8k = True  # type: ignore[attr-defined]
            logger.debug(
                "ElevenLabs ConvAI: native μ-law 8 kHz fast-path enabled "
                "(skipping inbound resample + outbound transcode)"
            )

        await self._adapter.connect()
        logger.debug("ElevenLabs ConvAI connected")

        self._background_task = asyncio.create_task(self._forward_events())

    async def _forward_events(self) -> None:
        # Arm first-byte capture so that the firstMessage turn (started in
        # start()) gets its tts_ms / total_ms recorded on the first audio
        # chunk. Parity with TS ``responseAudioStarted=false`` class field.
        waiting_first_audio = True
        current_agent_text = ""
        try:
            async for ev_type, ev_data in self._adapter.receive_events():
                if ev_type == "audio":
                    # Fallback: audio before speech_stopped. Parity with TS.
                    if self.metrics is not None and not self.metrics.turn_active:
                        self.metrics.start_turn()
                    if waiting_first_audio and self.metrics is not None:
                        self.metrics.record_tts_first_byte()
                        waiting_first_audio = False
                        # Speech-event: first TTS audio chunk for this turn.
                        # ConvAI is a fully-baked agent so the SDK doesn't see
                        # token-level LLM deltas; the audio edge is the only
                        # observable per-turn signal.
                        await self._emit_audio_out(tts_provider="elevenlabs_convai")
                    await self.audio_sender.send_audio(ev_data)

                elif ev_type == "speech_stopped":
                    # Start turn as soon as server VAD signals end-of-user-speech,
                    # not on transcript_input (which arrives later and understates latency).
                    if self.metrics is not None and not self.metrics.turn_active:
                        self.metrics.start_turn()
                    waiting_first_audio = True
                    current_agent_text = ""

                elif ev_type == "transcript_input":
                    logger.debug("User: %s", sanitize_log_value(ev_data))
                    if self.metrics is not None:
                        if not self.metrics.turn_active:
                            self.metrics.start_turn()
                        self.metrics.record_stt_complete(ev_data)
                    waiting_first_audio = True
                    current_agent_text = ""
                    self.conversation_history.append(
                        {"role": "user", "text": ev_data, "timestamp": time.time()}
                    )
                    self.transcript_entries.append({"role": "user", "text": ev_data})
                    if self.on_transcript:
                        await self.on_transcript(
                            {
                                "role": "user",
                                "text": ev_data,
                                "call_id": self.call_id,
                                "history": list(self.conversation_history),
                            }
                        )

                elif ev_type == "transcript_output":
                    if ev_data:
                        response_text: str = ev_data
                        # Speech-event: per-turn TTFT (LLM first token).
                        # ConvAI's WS streams the assistant transcript text
                        # alongside audio; the first delta is the earliest
                        # observable proxy for an LLM token.
                        await self._emit_llm_first_token(
                            llm_provider="elevenlabs_convai",
                            model=self.agent.model,
                        )
                        blocked, _ = evaluate_guardrails(self.agent, response_text)
                        if blocked:
                            current_agent_text = ""
                        else:
                            current_agent_text += response_text

                elif ev_type == "response_done":
                    if current_agent_text:
                        self.conversation_history.append(
                            {
                                "role": "assistant",
                                "text": current_agent_text,
                                "timestamp": time.time(),
                            }
                        )
                        self.transcript_entries.append(
                            {"role": "assistant", "text": current_agent_text}
                        )
                        if self.metrics is not None:
                            turn = self.metrics.record_turn_complete(current_agent_text)
                            await self._emit_turn_metrics(turn)
                        current_agent_text = ""
                    elif self.metrics is not None and self.metrics.turn_active:
                        # response_done without agent text = cancelled / empty.
                        # Close the active turn as interrupted — parity with TS.
                        self.metrics.record_turn_interrupted()
                    waiting_first_audio = True

                elif ev_type == "interruption":
                    await self.audio_sender.send_clear()
                    if self.metrics is not None:
                        self.metrics.record_turn_interrupted()
                    waiting_first_audio = False
                    current_agent_text = ""
        except Exception as exc:
            logger.exception("ElevenLabs ConvAI forward error: %s", exc)

    async def on_audio_received(self, audio_bytes: bytes) -> None:
        """Forward decoded telephony audio to ConvAI (μ-law fast-path or resampled PCM16)."""
        if self._adapter is None:
            return
        # Native μ-law 8 kHz fast-path: ConvAI negotiated ulaw_8000 on the
        # input side too, so the caller's μ-law bytes go through untouched.
        # No mulaw → PCM16 decode, no 8 kHz → 16 kHz resample.
        if self._native_mulaw_8k:
            await self._adapter.send_audio(audio_bytes)
            return
        # Default path: ConvAI expects PCM16 16 kHz and Twilio sends μ-law
        # 8 kHz, so decode + resample before forwarding.
        if self._for_twilio:
            from getpatter.audio.transcoding import mulaw_to_pcm16

            # Use per-handler StatefulResampler to preserve ratecv state.
            if self._resampler_8k_to_16k is None:
                from getpatter.audio.transcoding import create_resampler_8k_to_16k

                self._resampler_8k_to_16k = create_resampler_8k_to_16k()
            pcm16k = self._resampler_8k_to_16k.process(mulaw_to_pcm16(audio_bytes))
            await self._adapter.send_audio(pcm16k)
        else:
            await self._adapter.send_audio(audio_bytes)

    async def cleanup(self) -> None:
        """Cancel the event-forward task and close the ConvAI adapter."""
        if self._background_task:
            self._background_task.cancel()
            try:
                await self._background_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._adapter:
            await self._adapter.close()
        # Flush and discard the resampler tail on cleanup.
        if self._resampler_8k_to_16k is not None:
            self._resampler_8k_to_16k.flush()
            self._resampler_8k_to_16k = None


# ---------------------------------------------------------------------------
# Pipeline StreamHandler (STT -> LLM -> TTS)
# ---------------------------------------------------------------------------


class PipelineStreamHandler(StreamHandler):
    """Handles the pipeline provider mode (configurable STT + LLM + TTS)."""

    def __init__(
        self,
        agent,
        audio_sender: AudioSender,
        call_id: str,
        caller: str,
        callee: str,
        resolved_prompt: str,
        metrics,
        *,
        openai_key: str = "",
        deepgram_key: str = "",
        elevenlabs_key: str = "",
        for_twilio: bool = False,
        input_is_mulaw_8k: bool | None = None,
        output_is_mulaw_8k: bool | None = None,
        transfer_fn=None,
        hangup_fn=None,
        send_dtmf_fn=None,
        on_transcript=None,
        on_message=None,
        on_metrics=None,
        conversation_history: deque | None = None,
        transcript_entries: deque | None = None,
    ) -> None:
        super().__init__(
            agent=agent,
            audio_sender=audio_sender,
            call_id=call_id,
            caller=caller,
            callee=callee,
            resolved_prompt=resolved_prompt,
            metrics=metrics,
            on_transcript=on_transcript,
            on_message=on_message,
            on_metrics=on_metrics,
            conversation_history=conversation_history,
            transcript_entries=transcript_entries,
        )
        self._openai_key = openai_key
        self._deepgram_key = deepgram_key
        self._elevenlabs_key = elevenlabs_key
        self._for_twilio = for_twilio
        # Explicit codec flags decouple "we run on Twilio" (for metrics /
        # telephony-specific knobs) from "the stream is PCMU 8 kHz and must
        # be transcoded before STT / from PCM16 for TTS". Twilio is always
        # mulaw 8 kHz; Telnyx is mulaw 8 kHz when ``streaming_start``
        # negotiates PCMU bidirectional (our default). Callers pass the
        # flags explicitly when they differ from `for_twilio`.
        self._input_is_mulaw_8k = (
            for_twilio if input_is_mulaw_8k is None else input_is_mulaw_8k
        )
        self._output_is_mulaw_8k = (
            for_twilio if output_is_mulaw_8k is None else output_is_mulaw_8k
        )
        self._transfer_fn = transfer_fn
        self._hangup_fn = hangup_fn
        self._send_dtmf_fn = send_dtmf_fn
        self._stt = None
        self._tts = None
        # Auto-VAD: if ``agent.vad`` is None we attempt to load SileroVAD
        # with phone-friendly defaults during ``start()``. Stored separately
        # because ``agent`` is a frozen dataclass.
        self._auto_vad = None
        self._stt_task: asyncio.Task | None = None
        self._is_speaking = False
        # Per-turn LLM cancel event. Recreated on every new turn before LLM
        # consumption so a stale cancel from a previous turn cannot terminate
        # the next stream prematurely. Initialized here so the STT loop's
        # first turn (which references it via ``self._llm_cancel_event``
        # before any LLM consumption has run) does not AttributeError.
        self._llm_cancel_event: asyncio.Event = asyncio.Event()
        # Wall-clock timestamp (``time.time()`` units) of the last
        # ``_begin_speaking`` call. Cleared by the grace flip. Used by
        # ``_can_barge_in`` to suppress early self-cancellation while
        # the AEC filter is still converging (~500 ms warmup + safety
        # margin).
        self._speaking_started_at: float | None = None
        # Monotonic counter incremented at every TTS-start. ``_end_speaking_with_grace``
        # captures the value at scheduling time and only flips ``_is_speaking`` to
        # False if no new turn started in the meantime. Prevents an in-flight grace
        # task from clobbering the speaking flag of the *next* turn (mirrors TS).
        self._speaking_generation: int = 0
        # Ring buffer of inbound PCM16 16 kHz frames captured while the
        # agent is speaking and the self-hearing guard is dropping audio.
        # On barge-in we flush this buffer to STT so Deepgram (or any
        # other streaming STT) receives the user's first ~600 ms of
        # speech — which would otherwise be lost while the VAD's
        # ``min_speech_duration`` window accumulated and fired
        # ``speech_start``. Each frame is 20 ms × 32 bytes (16 kHz ×
        # 16-bit mono) ≈ 640 bytes; capped to 30 frames ≈ 600 ms ≈
        # ~19 KB per concurrent call.
        self._inbound_audio_ring: list[bytes] = []
        # Wall-clock timestamp of the most recent barge-in cancel, used by
        # ``_begin_speaking`` to enforce a short drain window so the remote
        # PSTN player finishes flushing the cancelled turn's tail before
        # the next TTS chunk lands on top of it. Without this, the first
        # sentence of a post-barge-in turn audibly overlaps with the tail
        # of the cancelled turn (~50-200 ms of doubled audio).
        self._last_cancel_at: float | None = None
        # Acoustic echo canceller, lazily instantiated in ``start()`` when
        # ``agent.echo_cancellation`` is set. ``None`` otherwise — the mic
        # path stays a pure pass-through for handset/headset deployments
        # that don't need it.
        self._aec = None
        # Task reference for the in-flight LLM-consumption loop.  Set by
        # ``_process_streaming_response`` and cancelled on barge-in so the
        # provider stops streaming tokens we will never speak — saves API
        # cost and frees the LLM connection slot earlier.
        self._llm_consume_task: asyncio.Task | None = None
        self._call_control = None
        self._llm_loop = None
        self._msg_accepts_call = False
        self._remote_handler = None
        # Throttle state for back-to-back STT finals — see ``_commit_transcript``.
        self._last_commit_text: str = ""
        self._last_commit_at: float = 0.0
        # Per-handler StatefulResampler for mulaw 8 kHz -> PCM16 16 kHz transcoding.
        self._resampler_8k_to_16k = None

    async def start(self) -> None:
        """Initialize STT/TTS providers, hooks, and start the STT receive loop."""
        from getpatter.models import CallControl

        # Create STT. Pipeline mode always transcodes Twilio mulaw 8 kHz →
        # PCM16 16 kHz in on_audio_received before forwarding to STT, so the
        # STT adapter must be configured for linear16 @ 16 kHz — even on
        # Twilio. Passing `for_twilio=True` would build a mulaw-expecting
        # adapter that misinterprets the already-decoded PCM as garbage.
        if self.agent.stt:
            self._stt = _create_stt_from_config(self.agent.stt, for_twilio=False)
        elif self._deepgram_key:
            from getpatter.providers.deepgram_stt import DeepgramSTT  # type: ignore[import]

            self._stt = DeepgramSTT(
                api_key=self._deepgram_key,
                language=self.agent.language,
                encoding="linear16",
                sample_rate=16000,
            )

        # Create TTS
        if self.agent.tts:
            self._tts = _create_tts_from_config(self.agent.tts)
        elif self._elevenlabs_key:
            from getpatter.providers.elevenlabs_tts import ElevenLabsTTS  # type: ignore[import]

            self._tts = ElevenLabsTTS(
                api_key=self._elevenlabs_key, voice_id=self.agent.voice
            )

        # Advise the TTS adapter of the telephony carrier so it can pick a
        # wire-native ``output_format`` (e.g. ``ulaw_8000`` on Twilio) and
        # skip a client-side transcode. The hook is opt-in per-adapter:
        # adapters that don't expose ``set_telephony_carrier`` keep their
        # constructed format. Adapters that do (e.g. ElevenLabsWebSocketTTS)
        # only auto-flip when the user did NOT explicitly pass output_format.
        if self._tts is not None and hasattr(self._tts, "set_telephony_carrier"):
            try:
                self._tts.set_telephony_carrier(
                    "twilio" if self._for_twilio else "telnyx"
                )
            except Exception:  # pragma: no cover - defensive; adapter bug
                logger.debug(
                    "TTS set_telephony_carrier failed; using construction-time format",
                    exc_info=True,
                )

        if self._stt is None:
            logger.warning("Pipeline mode: no STT configured")
        if self._tts is None:
            logger.warning("Pipeline mode: no TTS configured")

        # Auto-VAD: load SileroVAD with telephony-tuned defaults if the user
        # didn't pass one. Falls back silently to the STT-endpoint heuristic
        # when the ``silero`` extra is missing — same behaviour as before for
        # users who have not installed onnxruntime.
        if getattr(self.agent, "vad", None) is None:
            try:
                from getpatter.providers.silero_vad import SileroVAD

                self._auto_vad = await asyncio.to_thread(SileroVAD.for_phone_call)
                logger.info(
                    "auto-VAD enabled (SileroVAD, phone preset). Pass agent.vad=... to override."
                )
            except ImportError:
                logger.info(
                    "auto-VAD unavailable: onnxruntime/numpy not installed. "
                    "Install with `pip install getpatter[silero]` for fast barge-in."
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "auto-VAD load failed (%s); falling back to STT-endpoint heuristic",
                    exc,
                )

        # Acoustic echo cancellation: opt-in.
        #
        # Per the industry consensus (LiveKit, Pipecat, Vapi, Retell,
        # Bland) and Twilio's own guidance, time-domain NLMS server-side
        # AEC is the right tool only when the SDK has near-direct access
        # to the mic and speaker (browser WebRTC, mobile native). PSTN
        # paths route through a 250–1500 ms Twilio jitter buffer + carrier
        # loop — far outside the 32 ms window of a 512-tap NLMS filter at
        # 16 kHz, so the filter cannot model the echo and silently
        # degenerates into pass-through. Emit a warning so the operator
        # knows to either rely on the self-hearing guard alone (handset /
        # earpiece — minimal bleed) or keep AEC off (default) and tune
        # the VAD ``min_speech_duration`` if bleed-driven false positives
        # appear during firstMessage.
        if getattr(self.agent, "echo_cancellation", False):
            carrier = "twilio" if self._for_twilio else "telnyx"
            logger.warning(
                "echo_cancellation=True on %s (PSTN). Server-side NLMS "
                "cannot model PSTN's ~250-1500 ms round-trip echo with a "
                "32 ms filter window — it will silently no-op. Best "
                "practice: keep echo_cancellation=False; rely on the "
                "carrier + caller device's built-in echo suppression and "
                "Patter's self-hearing guard. Enable AEC only for "
                "browser/native deployments where the SDK owns the audio "
                "path end-to-end.",
                carrier,
            )
            try:
                from getpatter.audio.aec import NlmsEchoCanceller

                self._aec = NlmsEchoCanceller(sample_rate=16000)
                logger.info(
                    "echo cancellation enabled (NLMS, 512 taps + 0.5 s "
                    "warmup μ=0.5); filter converges within ~250 ms of TTS "
                    "playback in low-latency loops."
                )
            except ImportError:
                logger.warning(
                    "echo_cancellation=True but numpy is not installed; "
                    "install with `pip install getpatter[silero]` (numpy is part of that extra)."
                )

        if self._stt is not None:
            await self._stt.connect()

        logger.debug("Pipeline mode: STT + TTS connected")

        # Play first_message if configured and no on_message handler.
        # Measure TTS-first-byte latency for parity with TS (`stream-handler.ts`).
        if (
            self.agent.first_message
            and self.on_message is None
            and self._tts is not None
        ):
            if self.metrics is not None:
                self.metrics.start_turn()
            # Mark the agent as speaking for the duration of the first
            # message — without this, the self-hearing guard never
            # engages, the user's audio (mixed with TTS bleed) is
            # forwarded to STT and produces garbage transcripts, and
            # the ring buffer for pre-barge-in audio is never
            # populated. Mirrors the per-turn behaviour in
            # `_process_streaming_response` / `_process_regular_response`.
            await self._begin_speaking()
            first_chunk_sent = False
            # Drop any stale PCM16 carry byte from a prior synth (none at call
            # start, but defensive for parity with TS ``ttsByteCarry = null``).
            self.audio_sender.reset_pcm_carry()
            try:
                async for audio_chunk in self._tts.synthesize(self.agent.first_message):
                    if not self._is_speaking:
                        break  # barge-in or test-hangup
                    if not first_chunk_sent:
                        first_chunk_sent = True
                        if self.metrics is not None:
                            self.metrics.record_tts_first_byte()
                    # Far-end tap for the echo canceller — push the
                    # exact PCM the carrier-side encoder will transmit.
                    # Without this the AEC adapt loop has no reference
                    # signal during the intro, resulting in unmitigated
                    # bleed-through and a "first turn unresponsive" UX
                    # where the user's voice is masked by the agent's
                    # TTS in the inbound channel.
                    if self._aec is not None:
                        self._aec.push_far_end(audio_chunk)
                    await self.audio_sender.send_audio(audio_chunk)
            finally:
                # Drop any partial int16 byte to prevent cross-turn corruption
                # if the stream threw before a complete sample was delivered.
                self.audio_sender.reset_pcm_carry()
                # Flip back to not-speaking with grace so the ring
                # buffer accumulated during the intro is flushed and
                # the next user utterance is recognised cleanly.
                await self._end_speaking_with_grace()
            if first_chunk_sent and self.metrics is not None:
                turn = self.metrics.record_turn_complete(self.agent.first_message)
                self.conversation_history.append(
                    {
                        "role": "assistant",
                        "text": self.agent.first_message,
                        "timestamp": time.time(),
                    }
                )
                await self._emit_turn_metrics(turn)

        # CallControl for pipeline mode
        self._call_control = CallControl(
            call_id=self.call_id,
            caller=self.caller,
            callee=self.callee,
            telephony_provider="twilio" if self._for_twilio else "telnyx",
            _transfer_fn=self._transfer_fn,
            _hangup_fn=self._hangup_fn,
            _send_dtmf_fn=self._send_dtmf_fn,
        )

        # Check if on_message accepts CallControl
        if self.on_message is not None and callable(self.on_message):
            try:
                sig = inspect.signature(self.on_message)
                self._msg_accepts_call = len(sig.parameters) >= 2
            except (ValueError, TypeError):
                pass

        # Built-in LLM loop. Three paths:
        #   1. `agent.llm` set + `on_message` set → ValueError (caught early
        #      in serve(), but we re-assert here for belt-and-braces).
        #   2. `agent.llm` set → use the user-supplied LLMProvider; openai_key
        #      is not required.
        #   3. Otherwise fall back to the legacy OpenAI default (requires
        #      `openai_key`).
        agent_llm = getattr(self.agent, "llm", None)
        if agent_llm is not None and self.on_message is not None:
            raise ValueError(
                "Cannot pass both `llm=` on the agent and `on_message=` on serve(). "
                "Pick one — `llm=` for built-in LLMs, `on_message=` for custom logic."
            )

        if self.on_message is None and (agent_llm is not None or self._openai_key):
            from getpatter.services.llm_loop import LLMLoop
            from getpatter.tools.tool_executor import ToolExecutor

            tool_executor = ToolExecutor() if self.agent.tools else None
            llm_model = self.agent.model
            if "realtime" in llm_model:
                llm_model = "gpt-4o-mini"
            self._llm_loop = LLMLoop(
                openai_key=self._openai_key,
                model=llm_model,
                system_prompt=self.resolved_prompt,
                tools=self.agent.tools,
                tool_executor=tool_executor,
                llm_provider=agent_llm,
                metrics=self.metrics,
                event_bus=self._event_bus,
                disable_phone_preamble=getattr(
                    self.agent, "disable_phone_preamble", False
                ),
                on_tool_call=self._record_tool_call,
            )

        # Create remote message handler once if on_message is a remote URL
        from getpatter.services.remote_message import (
            RemoteMessageHandler,
            is_remote_url,
        )

        if is_remote_url(self.on_message):
            self._remote_handler = RemoteMessageHandler()

        # Start STT receive loop
        if self._stt is not None:
            self._stt_task = asyncio.create_task(self._stt_loop())

    def _build_hook_context(self) -> HookContext:
        """Build a HookContext for the current call state."""
        return HookContext(
            call_id=self.call_id,
            caller=self.caller,
            callee=self.callee,
            history=tuple(self.conversation_history),
        )

    async def _emit_assistant_transcript(self, text: str) -> None:
        """Push an assistant turn into history+transcript_entries and fire
        ``on_transcript`` so host applications observe pipeline-mode
        replies the same way they observe realtime-mode replies (mirrors
        :meth:`OpenAIRealtimeStreamHandler._flush_assistant_turn`).
        Caller is responsible for filtering empty strings.
        """
        self.conversation_history.append(
            {"role": "assistant", "text": text, "timestamp": time.time()}
        )
        self.transcript_entries.append({"role": "assistant", "text": text})
        if self.on_transcript is not None:
            await self.on_transcript(
                {
                    "role": "assistant",
                    "text": text,
                    "call_id": self.call_id,
                    "history": list(self.conversation_history),
                }
            )

    async def _record_tool_call(self, name: str, arguments: dict, result: Any) -> None:
        """Surface a tool invocation into the transcript timeline. Emits
        TWO events: one ``role=tool`` entry for the call and a second one
        for the result (mirrors realtime-mode's two ``_emit_tool_event``
        calls in :meth:`OpenAIRealtimeStreamHandler._forward_events`).
        Wired as the :class:`LLMLoop` ``on_tool_call`` observer for
        pipeline mode.
        """
        try:
            args_text = json.dumps(arguments or {})
        except (TypeError, ValueError):
            args_text = "{}"
        # Coerce non-string results (e.g. providers that return a dict) to
        # JSON for the transcript display; the LLM has already received
        # the executor's raw return value via the messages array.
        if result is None:
            result_text: str | None = None
        elif isinstance(result, str):
            result_text = result
        else:
            try:
                result_text = json.dumps(result)
            except (TypeError, ValueError):
                result_text = str(result)

        # 1) Call event — transcript shows ``name(args_json)``
        call_text = f"{name}({args_text})"
        self.conversation_history.append(
            {"role": "tool", "text": call_text, "timestamp": time.time()}
        )
        self.transcript_entries.append({"role": "tool", "text": call_text})
        if self.on_transcript is not None:
            await self.on_transcript(
                {
                    "role": "tool",
                    "text": call_text,
                    "call_id": self.call_id,
                    "tool_name": name,
                    "tool_args": arguments or {},
                    "tool_result": None,
                }
            )

        # 2) Result event — transcript shows ``name(...) → result`` (truncated)
        if result_text is not None:
            displayed = (
                result_text if len(result_text) <= 200 else result_text[:200] + "…"
            )
            res_text = f"{name}(...) → {displayed}"
            self.conversation_history.append(
                {"role": "tool", "text": res_text, "timestamp": time.time()}
            )
            self.transcript_entries.append({"role": "tool", "text": res_text})
            if self.on_transcript is not None:
                await self.on_transcript(
                    {
                        "role": "tool",
                        "text": res_text,
                        "call_id": self.call_id,
                        "tool_name": name,
                        "tool_args": arguments or {},
                        "tool_result": result_text,
                    }
                )

    async def _synthesize_sentence(
        self,
        sentence: str,
        hook_executor: PipelineHookExecutor,
        hook_ctx: HookContext,
        first_tts_chunk: list,
    ) -> bool:
        """Synthesize a single sentence through TTS with hooks. Returns False if interrupted."""
        if self._tts is None:
            return True

        # Apply text transforms before the beforeSynthesize hook
        transformed = sentence
        text_transforms = getattr(self.agent, "text_transforms", None)
        if text_transforms:
            for fn in text_transforms:
                transformed = fn(transformed)

        # beforeSynthesize hook (per-sentence)
        processed = await hook_executor.run_before_synthesize(transformed, hook_ctx)
        if processed is None:
            return True  # hook skipped this sentence, not an interruption

        _tts_span = start_span(
            SPAN_TTS,
            {
                "getpatter.tts.text_len": len(processed),
                "patter.call.id": self.call_id,
            },
        )
        _tts_span.__enter__()
        gen = self._tts.synthesize(processed)
        # Drop any stale PCM16 alignment carry byte between sentences — TTS
        # providers yield arbitrary-length chunks, so an odd byte from the
        # previous sentence would corrupt the first sample of this one.
        # Matches TS ``ttsByteCarry = null`` reset at each synth boundary.
        self.audio_sender.reset_pcm_carry()
        try:
            async for audio_chunk in gen:
                if not self._is_speaking:
                    return False  # caller handles interrupted metrics

                # afterSynthesize hook (per-chunk). The await may yield
                # control to the event loop long enough for VAD to fire
                # ``speech_start during TTS → BARGE-IN``, which calls
                # ``_cancel_speaking()`` and flips ``_is_speaking`` to
                # False. Re-check below before pushing the resulting
                # audio to the carrier — without this re-check, exactly
                # one trailing chunk (~20–100 ms of audio) would race
                # past the cancel and prolong the perceived "agent
                # didn't stop" window.
                processed_audio = await hook_executor.run_after_synthesize(
                    audio_chunk, processed, hook_ctx
                )
                if processed_audio is None:
                    continue  # hook discarded this chunk
                if not self._is_speaking:
                    return False  # barge-in fired during the hook await

                if first_tts_chunk[0] and self.metrics is not None:
                    self.metrics.record_tts_first_byte()
                    first_tts_chunk[0] = False
                    # Speech-event: per-turn first TTS audio chunk. Idempotent
                    # in the dispatcher; fires for the first sentence's first
                    # synthesized chunk per turn.
                    await self._emit_audio_out()
                if self._event_bus is not None:
                    self._event_bus.emit(
                        "tts_chunk",
                        {"bytes": len(processed_audio)},
                    )
                # Far-end tap for the echo canceller. ``processed_audio`` is
                # the exact PCM 16 kHz bytes that get transcoded + sent to
                # the carrier — i.e. the cleanest reference of "what the
                # speaker is about to play". Push BEFORE ``send_audio`` so a
                # very fast carrier echo is still seen by the next mic frame.
                if self._aec is not None:
                    self._aec.push_far_end(processed_audio)
                await self.audio_sender.send_audio(processed_audio)
        finally:
            await gen.aclose()
            _tts_span.__exit__(None, None, None)
            # Drop any partial int16 byte so cross-sentence corruption never
            # leaks past an exception / early return.
            self.audio_sender.reset_pcm_carry()
        return True

    async def _process_streaming_response(self, result, call_id: str) -> str:
        """Process a streaming (async generator) response through TTS with sentence chunking."""
        chunker = SentenceChunker(
            aggressive_first_flush=getattr(self.agent, "aggressive_first_flush", False),
            language=getattr(self.agent, "language", "en"),
        )
        full_response_parts: list[str] = []
        await self._begin_speaking()
        first_tts_chunk = [True]
        llm_first_token_sent = [True]  # Fix 5: track LLM TTFT

        hooks = getattr(self.agent, "hooks", None)
        hook_executor = PipelineHookExecutor(hooks)
        hook_ctx = self._build_hook_context()

        # Reset the per-turn LLM cancel event so a stale cancel from a
        # previous turn cannot terminate this stream prematurely.  The
        # event is *set* by ``_handle_barge_in`` to break out of the
        # consumption loop and close the generator (which propagates
        # cancellation into the LLM provider's HTTP/WS connection).
        self._llm_cancel_event = asyncio.Event()

        interrupted = False
        llm_error = False
        _llm_span = start_span(
            SPAN_LLM,
            {"patter.call.id": self.call_id},
        )
        _llm_span.__enter__()
        try:
            try:
                async for token in result:
                    if self._llm_cancel_event.is_set():
                        interrupted = True
                        break
                    full_response_parts.append(token)
                    # Fix 5: record LLM first-token (TTFT).
                    if llm_first_token_sent[0] and self.metrics is not None:
                        self.metrics.record_llm_first_token()
                        llm_first_token_sent[0] = False
                        # Speech-event: fire per-turn TTFT marker for SDK
                        # callback consumers. Idempotent in the dispatcher.
                        await self._emit_llm_first_token(
                            llm_provider=self._infer_llm_provider(),
                            model=self.agent.model,
                        )

                    sentences = chunker.push(token)
                    # Fix 3: mark first-sentence boundary for accurate tts_ms.
                    if sentences and self.metrics is not None and first_tts_chunk[0]:
                        self.metrics.record_llm_first_sentence()
                    for sentence in sentences:
                        if not self._is_speaking:
                            interrupted = True
                            break

                        blocked, guard_name = evaluate_guardrails(self.agent, sentence)
                        if blocked:
                            sentence = get_guardrail_replacement(self.agent, guard_name)

                        # Tier 2 — per-sentence after_llm transform. Runs
                        # between the chunker and TTS so PII redaction /
                        # persona overlay / refusal swap can edit individual
                        # sentences without buffering the full LLM response.
                        # Returning None drops the sentence silently.
                        if hook_executor.has_after_llm_sentence():
                            transformed = await hook_executor.run_after_llm_sentence(
                                sentence, hook_ctx
                            )
                            if transformed is None:
                                continue  # hook dropped this sentence
                            sentence = transformed

                        if not await self._synthesize_sentence(
                            sentence, hook_executor, hook_ctx, first_tts_chunk
                        ):
                            interrupted = True
                            break

                    if interrupted:
                        break
            except Exception as exc:
                llm_error = True
                chunker.reset()  # discard partial content on LLM error
                logger.exception("LLM streaming error: %s", exc)
                # Close the active turn as interrupted so the metrics accumulator
                # does not leak an open turn when LLM throws mid-stream.
                if self.metrics is not None and self.metrics.turn_active:
                    self.metrics.record_turn_interrupted()

            if self.metrics is not None:
                self.metrics.record_llm_complete()

            # Flush remaining text from chunker (skip if LLM errored)
            if not llm_error and not interrupted:
                for sentence in chunker.flush():
                    if not self._is_speaking:
                        interrupted = True
                        break

                    blocked, guard_name = evaluate_guardrails(self.agent, sentence)
                    if blocked:
                        sentence = get_guardrail_replacement(self.agent, guard_name)

                    if hook_executor.has_after_llm_sentence():
                        transformed = await hook_executor.run_after_llm_sentence(
                            sentence, hook_ctx
                        )
                        if transformed is None:
                            continue
                        sentence = transformed

                    if not await self._synthesize_sentence(
                        sentence, hook_executor, hook_ctx, first_tts_chunk
                    ):
                        interrupted = True
                        break
        finally:
            # Schedule the flip to idle. Keeps the speaking flag set during
            # the audio tail still playing on the carrier so STT echo on
            # the trailing samples doesn't look like a fresh user turn.
            await self._end_speaking_with_grace()
            # If a barge-in cut us off mid-stream, close the LLM generator
            # so the underlying HTTP/WS connection releases any tokens we
            # would never speak. Best-effort — generators that already
            # exhausted normally are no-ops on aclose().
            if interrupted and hasattr(result, "aclose"):
                try:
                    await result.aclose()
                except Exception:  # pragma: no cover - defensive
                    pass
            try:
                _llm_span.__exit__(None, None, None)
            except Exception:  # pragma: no cover - defensive
                pass

        response_text = "".join(full_response_parts)

        if not interrupted and not llm_error and response_text:
            if self.metrics is not None:
                self.metrics.record_tts_complete(response_text)
                turn = self.metrics.record_turn_complete(response_text)
                await self._emit_turn_metrics(turn, call_id=call_id)
        return response_text

    async def _process_regular_response(self, response_text: str, call_id: str) -> None:
        """Process a regular (non-streaming) response through TTS."""
        if self.metrics is not None:
            self.metrics.record_llm_complete()

        if not response_text:
            return

        # Guardrails check (pipeline mode — was previously missing)
        blocked, guard_name = evaluate_guardrails(self.agent, response_text)
        if blocked:
            response_text = get_guardrail_replacement(self.agent, guard_name)

        await self._emit_assistant_transcript(response_text)
        # Use sentence chunking + hooks for consistent behavior with streaming path
        hooks = getattr(self.agent, "hooks", None)
        hook_executor = PipelineHookExecutor(hooks)
        hook_ctx = self._build_hook_context()

        chunker = SentenceChunker()
        sentences = chunker.push(response_text) + chunker.flush()
        if not sentences:
            sentences = [response_text] if response_text else []

        await self._begin_speaking()
        first_tts_chunk = [True]
        interrupted = False
        try:
            for sentence in sentences:
                if not self._is_speaking:
                    interrupted = True
                    break
                if not await self._synthesize_sentence(
                    sentence, hook_executor, hook_ctx, first_tts_chunk
                ):
                    interrupted = True
                    break
        finally:
            # Schedule the flip to idle (see ``_process_streaming_response``).
            await self._end_speaking_with_grace()

        if not interrupted:
            if self.metrics is not None:
                self.metrics.record_tts_complete(response_text)
                turn = self.metrics.record_turn_complete(response_text)
                await self._emit_turn_metrics(turn, call_id=call_id)

    async def _handle_barge_in(self, transcript) -> None:
        """Caller spoke over in-flight TTS. Flip speaking flag, clear downstream
        audio, record interruption. Mirrors TS ``handleBargeIn``.
        """
        if not (transcript.text and self._is_speaking):
            return
        if not self._can_barge_in():
            # Same rationale as the VAD-path gate in ``on_audio_received``:
            # gate is 1.0 s with AEC (filter warmup) or 0.25 s without
            # (anti-flicker only). INFO so unexpected suppressions are
            # visible without enabling debug logs.
            aec_state = "on" if getattr(self, "_aec", None) is not None else "off"
            logger.info(
                "Barge-in transcript suppressed (agent speaking < gate, aec=%s)",
                aec_state,
            )
            return
        if self.metrics is not None:
            self.metrics.record_overlap_start()
            self.metrics.record_bargein_detected()
        logger.debug(
            "Barge-in: caller spoke over agent (%s)",
            sanitize_log_value(transcript.text[:40]),
        )
        with start_span(
            SPAN_BARGEIN,
            {"patter.call.id": self.call_id},
        ):
            self._is_speaking = False
            self._speaking_started_at = None
            # Record cancel timestamp so ``_begin_speaking`` can enforce
            # a short drain window before the next TTS chunk lands on
            # top of the cancelled turn's tail (avoids audible "doubled
            # audio" on the first sentence post-barge-in). Mirrors the
            # VAD-path cancel branch — both barge-in paths must set the
            # timestamp for the drain to be effective.
            self._last_cancel_at = time.time()
            # Signal the in-flight LLM-consumption loop to stop fetching
            # tokens. The consume loop checks ``_llm_cancel_event`` between
            # iterations and ``aclose()``s the generator on exit, freeing
            # the upstream HTTP/WS slot and stopping further token billing.
            cancel_event = getattr(self, "_llm_cancel_event", None)
            if cancel_event is not None:
                cancel_event.set()
            try:
                await self.audio_sender.send_clear()
            except Exception as exc:
                logger.debug("send_clear during barge-in failed: %s", exc)
            if self.metrics is not None:
                self.metrics.record_tts_stopped()
                self.metrics.record_turn_interrupted()
                self.metrics.record_overlap_end(was_interruption=True)

    def _commit_transcript(self, text: str) -> bool:
        """Dedup + throttle + hallucination filter for final STT transcripts.

        Mirrors TS ``commitTranscript``. Returns ``True`` if the transcript
        should be committed to a turn, ``False`` if it must be dropped.
        Drop reasons: common hallucinations, duplicate within 2 s, or any
        final within 500 ms of the previous one.
        """
        now = time.time()
        normalised = text.strip().lower()
        stripped = normalised.rstrip(".,!?;: ").strip()
        since_last = now - self._last_commit_at

        if stripped in _STT_HALLUCINATIONS or stripped == "":
            logger.debug("Dropped likely STT hallucination: %r", normalised[:40])
            return False
        if since_last < 2.0 and normalised == self._last_commit_text:
            logger.debug(
                "Dropped duplicate final transcript (%.1fs since last): %r",
                since_last,
                normalised[:40],
            )
            return False
        if since_last < 0.5:
            logger.debug(
                "Dropped back-to-back final transcript (%.2fs since last): %r",
                since_last,
                normalised[:40],
            )
            return False
        self._last_commit_text = normalised
        self._last_commit_at = now
        return True

    async def _stt_loop(self) -> None:
        # Throttle state lives on the instance so ``_commit_transcript`` can be
        # reused across iterations. See ``_commit_transcript`` for filter rules.
        try:
            async for transcript in self._stt.receive_transcripts():
                await self._handle_barge_in(transcript)
                # Fix 1: start STT latency timer on first partial transcript so
                # stt_ms measures from speech-start not final-transcript delivery.
                if transcript.text and self.metrics is not None:
                    self.metrics.start_turn_if_idle()
                # Emit fine-grained transcript events (additive — existing
                # ``on_transcript`` callback path is unchanged).
                if transcript.text and self._event_bus is not None:
                    self._event_bus.emit(
                        "transcript_partial"
                        if not transcript.is_final
                        else "transcript_final",
                        {
                            "text": transcript.text,
                            "is_final": bool(transcript.is_final),
                            "confidence": float(transcript.confidence or 0.0),
                        },
                    )
                # Gate LLM dispatch on either ``is_final`` or ``speech_final``.
                # Deepgram's ``speech_final`` is a faster end-of-utterance hint
                # that fires before ``is_final`` on each turn — accepting it
                # here removes ~300–700 ms of per-turn latency at parity with
                # the TS handler.
                if not (
                    (transcript.is_final or transcript.speech_final) and transcript.text
                ):
                    continue
                if not self._commit_transcript(transcript.text):
                    continue

                # Record one STT span per final transcript turn. The span is
                # short-lived (just the attribute set) because STT is
                # streaming — we do not re-wrap the long-lived iterator.
                with start_span(
                    SPAN_STT,
                    {
                        "getpatter.stt.text_len": len(transcript.text),
                        "getpatter.stt.confidence": float(transcript.confidence or 0.0),
                        "patter.call.id": self.call_id,
                    },
                ):
                    pass

                logger.debug("User: %s", sanitize_log_value(transcript.text))

                if self.metrics is not None:
                    self.metrics.start_turn_if_idle()  # turn may already be open
                    # Known limitation: per-turn audio_seconds is not tracked
                    # here; metrics rely on total _stt_byte_count plus the
                    # end_call() estimation pass.
                    self.metrics.record_vad_stop()
                    self.metrics.record_stt_complete(transcript.text)
                    self.metrics.record_stt_final_timestamp()

                # Endpoint span — silence-detected → LLM-dispatch window. Open
                # here (right after VAD stop / final transcript is recorded)
                # and close it just before ``record_turn_committed`` below.
                endpoint_span = start_span(
                    SPAN_ENDPOINT,
                    {"patter.call.id": self.call_id},
                )
                endpoint_span.__enter__()
                # Wrapped in a list so the closure-style helper can flip the
                # flag without needing ``nonlocal`` (we are inside a loop body,
                # not a nested function — ``nonlocal`` would not bind here).
                _endpoint_closed = [False]

                def _close_endpoint_span() -> None:
                    if _endpoint_closed[0]:
                        return
                    _endpoint_closed[0] = True
                    try:
                        endpoint_span.__exit__(None, None, None)
                    except Exception:  # pragma: no cover - defensive
                        pass

                # Raw transcript always goes to dashboard/transcript log
                self.transcript_entries.append(
                    {"role": "user", "text": transcript.text}
                )

                if self.on_transcript:
                    await self.on_transcript(
                        {
                            "role": "user",
                            "text": transcript.text,
                            "call_id": self.call_id,
                            "history": list(self.conversation_history),
                        }
                    )

                # --- afterTranscribe hook ---
                hooks = getattr(self.agent, "hooks", None)
                hook_executor = PipelineHookExecutor(hooks)
                hook_ctx = self._build_hook_context()
                filtered_text = await hook_executor.run_after_transcribe(
                    transcript.text, hook_ctx
                )
                if filtered_text is None:
                    logger.debug("afterTranscribe hook vetoed turn")
                    if self.metrics is not None:
                        self.metrics.record_turn_interrupted()
                    _close_endpoint_span()
                    continue

                if self.metrics is not None:
                    self.metrics.record_on_user_turn_completed_delay(0.0)
                if self.on_message is None and self._llm_loop is None:
                    # No message handler or LLM loop — discard orphaned turn
                    if self.metrics is not None:
                        self.metrics.record_turn_interrupted()
                    _close_endpoint_span()
                    continue

                # Use filtered text in conversation history (sent to LLM)
                self.conversation_history.append(
                    {"role": "user", "text": filtered_text, "timestamp": time.time()}
                )

                # Built-in LLM loop path
                if self.on_message is None and self._llm_loop is not None:
                    call_ctx = {
                        "call_id": self.call_id,
                        "caller": self.caller,
                        "callee": self.callee,
                    }
                    if self.metrics is not None:
                        self.metrics.record_turn_committed()
                    _close_endpoint_span()
                    result = self._llm_loop.run(
                        filtered_text,
                        list(self.conversation_history),
                        call_ctx,
                        hook_executor=hook_executor,
                        hook_ctx=hook_ctx,
                        cancel_event=self._llm_cancel_event,
                    )
                    response_text = await self._process_streaming_response(
                        result, self.call_id
                    )
                    if response_text:
                        await self._emit_assistant_transcript(response_text)
                    continue

                # on_message handler path
                if self.metrics is not None:
                    self.metrics.record_turn_committed()
                _close_endpoint_span()
                msg_data = {
                    "text": filtered_text,
                    "call_id": self.call_id,
                    "caller": self.caller,
                    "callee": self.callee,
                    "history": list(self.conversation_history),
                }

                response_text = ""
                streaming = False

                from getpatter.services.remote_message import (
                    is_remote_url,
                    is_websocket_url,
                )

                if is_remote_url(self.on_message):
                    remote = self._remote_handler
                    if is_websocket_url(self.on_message):
                        result = remote.call_websocket(self.on_message, msg_data)
                        streaming = True
                    else:
                        response_text = await remote.call_webhook(
                            self.on_message, msg_data
                        )
                        streaming = False
                elif self._msg_accepts_call:
                    result = self.on_message(msg_data, self._call_control)
                else:
                    result = self.on_message(msg_data)

                if not is_remote_url(self.on_message):
                    if asyncio.iscoroutine(result):
                        response_text = await result
                        streaming = False
                    elif inspect.isasyncgen(result):
                        streaming = True
                    else:
                        response_text = result
                        streaming = False

                # Check if handler ended the call
                if self._call_control is not None and self._call_control.ended:
                    return

                if streaming:
                    response_text = await self._process_streaming_response(
                        result, self.call_id
                    )
                    if response_text:
                        await self._emit_assistant_transcript(response_text)
                else:
                    if not response_text:
                        # Common misuse: on_message was provided as an observer
                        # (returning None) but it actually replaces the built-in LLM
                        # loop. Warn loudly — the caller hears no audio until the
                        # handler returns a non-empty string.
                        logger.warning(
                            "on_message returned empty/None — no TTS will play. "
                            "If you intended to observe transcripts, use on_transcript "
                            "instead; if you meant to answer via the built-in LLM, "
                            "remove on_message and pass openai_key."
                        )
                    await self._process_regular_response(response_text, self.call_id)

        except Exception as exc:
            logger.exception("Pipeline STT loop error: %s", exc)

    async def on_audio_received(self, audio_bytes: bytes) -> None:
        """Forward caller audio to STT (transcoding to PCM16 16 kHz, running VAD/hooks)."""
        if self._stt is None:
            return
        # Always forward caller audio to STT — even while the agent is
        # speaking — so barge-in detection can trigger. When
        # ``barge_in_threshold_ms == 0`` on the agent, skip STT during TTS
        # to avoid echo-loop costs (opt-out for noisy links).
        if self._is_speaking and getattr(self.agent, "barge_in_threshold_ms", 300) == 0:
            return
        # Inbound PCMU 8 kHz (Twilio always, Telnyx when streaming_start
        # negotiated PCMU bidirectional) must be decoded to PCM16 and
        # up-sampled to 16 kHz before hitting STT adapters configured for
        # linear16 @ 16 kHz.
        if self._input_is_mulaw_8k:
            from getpatter.audio.transcoding import mulaw_to_pcm16

            # Use per-handler StatefulResampler to preserve ratecv filter state
            # across audio chunks (prevents boundary artefacts at STT input).
            if self._resampler_8k_to_16k is None:
                from getpatter.audio.transcoding import create_resampler_8k_to_16k

                self._resampler_8k_to_16k = create_resampler_8k_to_16k()
            pcm = self._resampler_8k_to_16k.process(mulaw_to_pcm16(audio_bytes))
        else:
            pcm = audio_bytes

        # ---- AEC ---- subtract estimated TTS bleed before VAD/STT see it.
        # Pass-through until the canceller has enough far-end history to
        # fill its filter window (~128 ms), then converges over the next
        # 0.5–2 s of TTS-only frames.
        if self._aec is not None:
            pcm = self._aec.process_near_end(pcm)

        # ---- VAD wiring (Fix 8) ----
        # Optional ``agent.vad`` (or auto-loaded SileroVAD when the user
        # didn't pass one) runs *before* STT so we can react to speech_start
        # with immediate barge-in (clearing the carrier audio buffer) rather
        # than waiting for the STT engine's slower endpoint.
        vad = getattr(self.agent, "vad", None) or self._auto_vad
        if vad is not None:
            try:
                vad_event = await vad.process_frame(pcm, 16000)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("VAD process_frame failed: %s", exc)
                vad_event = None
            if vad_event is not None:
                if vad_event.type == "speech_start":
                    if self._is_speaking and not self._can_barge_in():
                        # Within the per-turn warmup gate. With AEC on
                        # this is the ~1 s filter convergence window;
                        # without AEC it is just a 0.25 s anti-flicker
                        # margin. INFO so unexpected suppressions are
                        # visible without enabling debug logs.
                        aec_state = (
                            "on" if getattr(self, "_aec", None) is not None else "off"
                        )
                        logger.info(
                            "VAD speech_start suppressed (agent speaking < gate, aec=%s)",
                            aec_state,
                        )
                    elif self._is_speaking:
                        # Caller spoke over in-flight TTS — preempt now.
                        if self.metrics is not None:
                            self.metrics.record_bargein_detected()
                        with start_span(
                            SPAN_BARGEIN,
                            {"patter.call.id": self.call_id},
                        ):
                            try:
                                await self.audio_sender.send_clear()
                            except Exception as exc:
                                logger.debug(
                                    "send_clear during VAD barge-in failed: %s", exc
                                )
                            # Replay the ring buffer of inbound frames
                            # captured while the agent was speaking —
                            # see ``_flush_inbound_audio_ring`` for the
                            # full rationale.
                            await self._flush_inbound_audio_ring()
                            if self.metrics is not None:
                                self.metrics.record_tts_stopped()
                                self.metrics.record_turn_interrupted()
                            # Force-flip immediately and bump the generation so a
                            # pending grace-flip from the prior turn can't fight us.
                            self._is_speaking = False
                            self._speaking_started_at = None
                            self._speaking_generation += 1
                            # Record cancel timestamp so ``_begin_speaking``
                            # can enforce a short drain window before the
                            # next TTS chunk lands on top of the cancelled
                            # turn's tail (avoids audible "doubled audio"
                            # on the first sentence post-barge-in).
                            self._last_cancel_at = time.time()
                    if self.metrics is not None:
                        self.metrics.start_turn_if_idle()
                elif vad_event.type == "speech_end":
                    if self.metrics is not None:
                        self.metrics.record_vad_stop()
                    # The SDK's VAD has detected end-of-speech earlier
                    # and more reliably than the provider's own
                    # endpointing on PSTN (Deepgram natural-pause
                    # endpointing can run 1-6 s before it emits a
                    # final). Ask the provider to finalise the
                    # in-flight utterance NOW so the next turn can
                    # dispatch immediately. ``getattr`` so STT
                    # adapters that don't implement it (Whisper-class
                    # one-shot transcribers) simply skip.
                    finalize = getattr(self._stt, "finalize", None)
                    if callable(finalize):
                        try:
                            ret = finalize()
                            if asyncio.iscoroutine(ret):
                                await ret
                        except Exception as exc:  # pragma: no cover - defensive
                            logger.debug("STT finalize threw: %s", exc)

            # Self-hearing guard: while the agent is speaking, don't pass
            # caller audio to STT — VAD already gave us authoritative
            # barge-in detection above, so any STT audio sent here would
            # just be the agent's own TTS echoing back.
            #
            # Pre-barge-in buffer: instead of dropping the frame on the
            # floor, push it into a small ring (last ~600 ms). On a
            # future BARGE-IN this ring is flushed to STT so the user's
            # first words — captured BEFORE the VAD's
            # ``min_speech_duration`` window let it emit ``speech_start``
            # — actually reach Deepgram. Without this buffer, short
            # interruptions ("stop") never produced a transcript and the
            # agent kept talking; long ones produced truncated
            # transcripts and the agent answered to fragments.
            if self._is_speaking:
                self._inbound_audio_ring.append(pcm)
                # Cap to ~250 ms (matching SileroVAD ``min_speech_duration``)
                # so the post-barge-in replay only recovers the VAD-missed
                # leading edge of the user's speech, not ~350 ms of
                # pre-speech silence/agent-bleed. On PSTN (where AEC is a
                # no-op) Deepgram trained on English transcribes that
                # bleed as English garbage and commits it to the LLM as
                # a phantom user transcript. See BUGS.md 2026-05-05
                # post-barge-in bleed-transcription entry.
                if len(self._inbound_audio_ring) > 13:  # ~260 ms at 20 ms/frame
                    self._inbound_audio_ring.pop(0)
                return

        # before_send_to_stt hook — gate/transform the audio chunk before it
        # reaches the STT provider. Returning None drops the chunk (useful
        # for custom VAD / echo-cancellation / PII redaction).
        hooks = getattr(self.agent, "hooks", None)
        if hooks is not None:
            hook_executor = PipelineHookExecutor(hooks)
            hook_ctx = self._build_hook_context()
            processed = await hook_executor.run_before_send_to_stt(pcm, hook_ctx)
            if processed is None:
                return
            pcm = processed

        await self._stt.send_audio(pcm)
        if self.metrics is not None:
            # Count bytes that actually reach the STT adapter. When the
            # input is mulaw 8 kHz (Twilio / Telnyx PCMU), ``audio_bytes``
            # is 1B/sample @ 8 kHz — but the metrics layer is configured
            # for 16-bit @ 16 kHz, so counting the raw mulaw payload
            # under-reports STT seconds by 4x. Use ``pcm`` (post-decode,
            # post-resample) so the byte count matches the configured
            # STT format.
            self.metrics.add_stt_audio_bytes(len(pcm))

    # ---------------------------------------------------------------
    # TTS speaking state helpers (Fix 9)
    # ---------------------------------------------------------------

    # Minimum drain window (seconds) between a barge-in cancel and the
    # next ``_begin_speaking``. 0.15 s covers a typical PSTN jitter
    # buffer drain + Twilio Media Stream clear propagation. Lower values
    # risk audio overlap on the first chunk; higher values increase the
    # perceived "agent ack" latency after a barge-in. Mirrors TS
    # ``StreamHandler.POST_CANCEL_DRAIN_MS``.
    _POST_CANCEL_DRAIN_S: float = 0.15

    async def _begin_speaking(self) -> None:
        """Mark TTS playback as in-progress and bump the generation counter.

        Awaits the post-cancel drain window before flipping state so the
        remote PSTN player has time to flush the cancelled turn's tail.

        The generation counter is consulted by ``_end_speaking_with_grace``
        so a delayed flip-to-idle from a previous turn cannot cancel the
        speaking flag of the *current* turn.
        """
        if self._last_cancel_at is not None:
            elapsed = time.time() - self._last_cancel_at
            remaining = self._POST_CANCEL_DRAIN_S - elapsed
            if remaining > 0:
                await asyncio.sleep(remaining)
        self._speaking_generation += 1
        self._is_speaking = True
        self._speaking_started_at = time.time()
        # Fresh turn — drop any stale pre-barge-in buffer from a previous
        # turn so we never replay yesterday's audio to STT.
        self._inbound_audio_ring = []

    def _can_barge_in(self) -> bool:
        """Whether barge-in is allowed to fire right now.

        Gate length depends on whether AEC is active: 1 s with AEC
        (covers filter warmup), 0.25 s without (anti-flicker only —
        keeps PSTN barge-in responsive, since on PSTN AEC is a no-op
        and there is no warmup to protect).

        ``getattr`` is used so test fixtures that flip ``_is_speaking``
        directly (without going through ``_begin_speaking``) still
        permit barge-in to fire.
        """
        started_at = getattr(self, "_speaking_started_at", None)
        if started_at is None:
            return True
        elapsed = time.time() - started_at
        gate = (
            MIN_AGENT_SPEAKING_S_BEFORE_BARGE_IN_AEC
            if getattr(self, "_aec", None) is not None
            else MIN_AGENT_SPEAKING_S_BEFORE_BARGE_IN_NO_AEC
        )
        return elapsed >= gate

    async def _end_speaking_with_grace(self) -> None:
        """Flip ``_is_speaking`` to False after a configurable grace period.

        TTS adapters typically signal "stream complete" while the carrier is
        still playing the tail of the last audio chunk. Resetting the flag
        immediately allows STT hallucinations on TTS echo to look like a
        fresh user turn. The grace window — controlled via
        ``PATTER_TTS_TAIL_GRACE_MS`` (default 1500 ms) — keeps the flag set
        while the trailing audio actually plays out. Setting the env var to
        ``0`` keeps the legacy synchronous behaviour for tests / soak runs.
        """
        try:
            grace_ms = int(os.environ.get("PATTER_TTS_TAIL_GRACE_MS", "1500"))
        except ValueError:
            grace_ms = 1500
        # NOTE: we do NOT flush ``_inbound_audio_ring`` here — the ring is
        # only drained on a real barge-in (where VAD confirmed user speech).
        # Flushing on every natural turn end was tried in an earlier
        # iteration and caused garbled out-of-order responses: the ring
        # captured during the agent's TTS contains audio with partially
        # cancelled echo and possibly over-cancelled user voice (Geigel
        # rho=0.6 misses quiet double-talk). Replaying that to STT on every
        # turn produced phantom transcripts that raced live STT input and
        # confused the LLM. Audio captured during the agent's turn that VAD
        # did NOT classify as speech is intentionally dropped at the next
        # ``_begin_speaking()``.
        if grace_ms <= 0:
            self._is_speaking = False
            self._speaking_started_at = None
            return

        gen = self._speaking_generation

        async def _flip_after_grace() -> None:
            try:
                await asyncio.sleep(grace_ms / 1000)
                # Only reset if no newer turn started while we slept; a
                # newer turn would have bumped ``_speaking_generation``.
                if self._speaking_generation == gen:
                    self._is_speaking = False
                    self._speaking_started_at = None
            except asyncio.CancelledError:  # pragma: no cover
                raise
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("tts grace flip failed: %s", exc)

        asyncio.create_task(_flip_after_grace())

    async def _flush_inbound_audio_ring(self) -> None:
        """Replay the audio captured by the self-hearing guard right
        before a confirmed barge-in.

        VAD's ``min_speech_duration`` window (default 250 ms) means
        ``speech_start`` fires only AFTER the user has been talking
        for that long; without this replay STT sees only the tail of
        the user's interruption and produces "the line is breaking up"
        partial transcripts. We deliberately do NOT call this on
        natural turn end — see the comment in
        ``_end_speaking_with_grace`` for why.
        """
        if self._stt is None or not self._inbound_audio_ring:
            return
        replayed = len(self._inbound_audio_ring)
        for buf in self._inbound_audio_ring:
            try:
                await self._stt.send_audio(buf)
            except Exception as exc:
                logger.debug("send_audio replay failed: %s", exc)
        self._inbound_audio_ring = []
        logger.debug(
            "Flushed %d pre-turn-end frame(s) (~%d ms) to STT",
            replayed,
            replayed * 20,
        )

    async def cleanup(self) -> None:
        """Cancel the STT loop and close STT/TTS/remote-message adapters."""
        if self._stt_task:
            self._stt_task.cancel()
            try:
                await self._stt_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._stt is not None:
            await self._stt.close()
        if self._tts is not None:
            await self._tts.close()
        if self._remote_handler is not None:
            await self._remote_handler.close()
        # Flush and discard the inbound resampler tail on cleanup.
        if self._resampler_8k_to_16k is not None:
            self._resampler_8k_to_16k.flush()
            self._resampler_8k_to_16k = None

    @property
    def stt(self):
        """Expose STT adapter for post-call metrics queries."""
        return self._stt


# ---------------------------------------------------------------------------
# Shared post-call metrics helpers
# ---------------------------------------------------------------------------


async def fetch_deepgram_cost(metrics, stt, deepgram_key: str) -> None:
    """Query Deepgram API for actual STT cost after a call ends."""
    if (
        metrics is None
        or stt is None
        or not deepgram_key
        or not hasattr(stt, "request_id")
        or not stt.request_id
    ):
        return
    try:
        import httpx as _httpx

        async with _httpx.AsyncClient() as http:
            proj_resp = await http.get(
                "https://api.deepgram.com/v1/projects",
                headers={"Authorization": f"Token {deepgram_key}"},
                timeout=5.0,
            )
            if proj_resp.status_code == 200:
                projects = proj_resp.json().get("projects", [])
                if projects:
                    project_id = projects[0].get("project_id", "")
                    if project_id:
                        req_resp = await http.get(
                            f"https://api.deepgram.com/v1/projects/{project_id}/requests/{stt.request_id}",
                            headers={"Authorization": f"Token {deepgram_key}"},
                            timeout=5.0,
                        )
                        if req_resp.status_code == 200:
                            usd = (
                                req_resp.json()
                                .get("response", {})
                                .get("details", {})
                                .get("usd", None)
                            )
                            if usd is not None:
                                metrics.set_actual_stt_cost(float(usd))
                                logger.debug("Deepgram actual cost: $%s", usd)
    except Exception as exc:
        logger.debug("Could not fetch Deepgram request cost: %s", exc)
