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
from typing import TYPE_CHECKING, Any

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


from getpatter.utils.ws import is_ws_alive as _is_parked_ws_alive  # noqa: E402


# Minimum wall-clock duration (seconds) the agent must have been speaking
# before barge-in is allowed to fire. AEC variant (1.0 s) covers the
# filter convergence window. NO_AEC variant raised 0.1 → 0.5 s on
# 2026-05-19 after the 0.6.2 acceptance run showed a phantom VAD
# speech_start firing on the very first inbound frame, cancelling the
# prewarmed firstMessage and leaving the turn-state machine wedged
# (``_turn_already_closed=True``). 0.5 s filters those phantoms while
# still allowing real interruptions to land within half a second of
# agent onset.
MIN_AGENT_SPEAKING_S_BEFORE_BARGE_IN_AEC = 1.0
MIN_AGENT_SPEAKING_S_BEFORE_BARGE_IN_NO_AEC = 0.5
# Backwards-compat alias used by tests; matches AEC variant.
MIN_AGENT_SPEAKING_S_BEFORE_BARGE_IN = MIN_AGENT_SPEAKING_S_BEFORE_BARGE_IN_AEC


# ---------------------------------------------------------------------------
# Shared tool definitions injected into every agent
# ---------------------------------------------------------------------------

# Short words / phrases that Whisper (and, less often, Deepgram) routinely
# emit when fed silence or TTS echo on mulaw 8 kHz. Dropping them as turns
# prevents the caller from entering a feedback loop where every silent frame
# triggers a new LLM+TTS turn. Parity with TS ``HALLUCINATIONS``.
#
# Whisper-specific full-phrase hallucinations (the model's training set
# was dominated by YouTube captions — on silence / echo it falls back to
# the most common training-set closers). These fire HARD on PSTN echo
# loopback when the agent's outbound audio bleeds into the input buffer
# and the upstream VAD commits a "non-empty" segment to transcription.
# Comparison happens against the lower-cased + stripped form, so add
# the canonical lowercase spelling here.
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
        # Whisper YouTube-caption hallucinations
        "thank you for watching",
        "thanks for watching",
        "thank you for watching!",
        "thanks for watching!",
        "thank you so much for watching",
        "thanks for listening",
        "please subscribe",
        "subscribe",
        "music",
        "[music]",
        "♪",
        "[no audio]",
        "[silence]",
        "[blank_audio]",
        "(silence)",
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


def _augment_with_builtin_handoff_tools(
    user_tools: list[dict] | None,
    *,
    transfer_fn: Any | None,
    hangup_fn: Any | None,
) -> list[dict]:
    """Return ``user_tools`` with the built-in ``transfer_call`` and
    ``end_call`` tools appended, each wired with a handler closure that
    routes to the telephony-level ``_transfer_fn`` / ``_hangup_fn``
    already attached to the stream handler.

    Used by pipeline mode to match the realtime path's tool surface
    (see ``OpenAIRealtimeStreamHandler.start`` where the same two
    built-ins are injected into ``session.update``). Without this the
    pipeline LLM never sees the built-in tools and cannot initiate a
    transfer or hangup regardless of system-prompt instructions.

    Tools are appended (not prepended) so user-provided tools keep their
    original order. The handler signature ``(arguments, call_context)``
    matches the calling convention used by ``ToolExecutor._invoke_handler``.
    """
    out: list[dict] = list(user_tools or [])
    if transfer_fn is not None:

        async def _transfer_handler(arguments: dict, call_context: dict) -> str:
            number = (arguments or {}).get("number", "")
            await transfer_fn(number)
            return f"Transferring to {number}" if number else "Transfer rejected"

        out.append({**TRANSFER_CALL_TOOL, "handler": _transfer_handler})
    if hangup_fn is not None:

        async def _hangup_handler(arguments: dict, call_context: dict) -> str:
            await hangup_fn()
            return "Call ended"

        out.append({**END_CALL_TOOL, "handler": _hangup_handler})
    return out


def _inject_consult_tool(agent):
    """Return *agent* with the built-in ``consult`` tool merged into its tool
    list when ``agent.consult`` is set; otherwise return *agent* unchanged.

    Mirrors :meth:`_init_mcp_tools` — ``Agent`` is frozen, so a copy with the
    merged tools is returned via :func:`dataclasses.replace`. Called from both
    the Realtime and Pipeline start paths so the consult tool's schema reaches
    the model and its handler reaches the ``ToolExecutor`` uniformly. Idempotent:
    a no-op if a tool with the same name is already present.
    """
    consult = getattr(agent, "consult", None)
    if consult is None:
        return agent
    from getpatter.tools.consult import build_consult_tool

    consult_tool = build_consult_tool(consult)
    existing = list(agent.tools or [])
    if any(
        isinstance(t, dict) and t.get("name") == consult_tool["name"] for t in existing
    ):
        return agent
    import dataclasses

    return dataclasses.replace(agent, tools=tuple(existing) + (consult_tool,))


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
    elif provider in ("openai_realtime", "openai_realtime_2"):
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
    if provider in ("openai_realtime", "openai_realtime_2"):
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


async def _safe_close_parked_handle(handle: object) -> None:
    """Best-effort async close of a parked provider handle that the
    StreamHandler chose NOT to adopt (cache miss, parked WS already
    dead, unknown shape, etc.).

    Handles all flavours used by the SDK:
    - tuple ``(session, ws)`` from Cartesia STT.
    - object with ``.ws`` attribute (e.g. ``ElevenLabsParkedWS``).
    - bare WebSocket / ``WebSocketClientProtocol``.
    """
    try:
        if isinstance(handle, tuple) and len(handle) == 2:
            session, ws = handle
            try:
                await ws.close()
            except Exception:
                pass
            try:
                await session.close()
            except Exception:
                pass
            return
        ws = getattr(handle, "ws", None)
        if ws is not None:
            await ws.close()
            return
        await handle.close()  # type: ignore[attr-defined]
    except Exception:
        pass


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

        # Set by Patter._attach_span_exporter via attach_span_exporter; "uut" by default.
        # Read once at handler start; later changes via the same Patter instance
        # will not retroactively affect this handler's spans.
        self._patter_side: str = getattr(self, "_patter_side", "uut")

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

        self.agent = dataclasses.replace(self.agent, tools=tuple(existing + discovered))
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
        # Stamp patter.latency.{ttfb_ms,turn_ms} on the active span before the
        # user callback runs. ``ttfb_ms`` maps to ``total_ms`` (turn_start →
        # first TTS audio byte — the user-perceptible "time to first byte"
        # for the response). ``turn_ms`` maps to ``tts_total_ms`` when set
        # (LLM-first-token → last TTS byte) and falls back to ``total_ms``.
        if turn is not None and getattr(turn, "latency", None) is not None:
            try:
                from getpatter.services.pipeline_hooks import PipelineHookExecutor

                ttfb_ms = float(turn.latency.total_ms or 0.0)
                turn_ms = float(
                    turn.latency.tts_total_ms
                    if turn.latency.tts_total_ms is not None
                    else (turn.latency.total_ms or 0.0)
                )
                PipelineHookExecutor(hooks=None).record_turn_latency(
                    ttfb_ms=ttfb_ms, turn_ms=turn_ms
                )
            except Exception:  # pragma: no cover — observability must never break calls
                logger.debug("record_turn_latency failed", exc_info=True)

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
        pop_prewarmed_connections=None,
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
        # Callback supplied by the telephony adapter so we can adopt a
        # Realtime WS that ``Patter._park_provider_connections`` opened
        # during the ringing window. ``None`` skips adoption — we fall
        # back to a cold ``connect()``.
        self._pop_prewarmed_connections = pop_prewarmed_connections
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
        # Both ``openai_realtime`` and ``openai_realtime_2`` engines now
        # route through the GA-compatible ``OpenAIRealtime2Adapter`` —
        # OpenAI deprecated the Beta Realtime API on 2026-05, returning
        # `invalid_model` to the legacy ``session.update`` shape and the
        # ``OpenAI-Beta: realtime=v1`` header. Only the default model
        # string differs between the two engines (mini vs flagship);
        # everything else (session shape, MIME types, event names) is
        # identical and lives in the GA adapter.
        from getpatter.providers.openai_realtime_2 import (  # type: ignore[import]
            OpenAIRealtime2Adapter,
        )

        _adapter_cls = OpenAIRealtime2Adapter

        # Resolve MCP servers BEFORE the adapter is built so the
        # discovered tools are visible in the first ``session.update``.
        # Failures are logged but not fatal — a dead MCP server should
        # not kill the entire call. Parity with TS ``initMcpTools``.
        await self._init_mcp_tools()
        # Merge the built-in consult tool (if configured) so its schema reaches
        # the Realtime session and its handler reaches the ToolExecutor.
        self.agent = _inject_consult_tool(self.agent)

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
        self._adapter = _adapter_cls(**adapter_kwargs)

        # Try to adopt a Realtime WebSocket parked during the ringing
        # window. When present we skip the cold ``connect()`` — the
        # parked socket has already paid the TCP + TLS + HTTP-101 +
        # ``session.update`` ack round-trip (~300-600 ms saved on first
        # audible word). Fall back transparently on cache miss / dead
        # socket / adapter missing ``adopt_websocket``.
        parked: dict | None = None
        pop_cb = self._pop_prewarmed_connections
        if pop_cb is None:
            logger.info(
                "[PREWARM] callId=%s provider=openai_realtime SKIPPED adoption: "
                "pop_prewarmed_connections callback not wired",
                self.call_id,
            )
        else:
            try:
                parked = pop_cb(self.call_id)
            except Exception as exc:  # noqa: BLE001 - best-effort
                logger.info(
                    "[PREWARM] callId=%s provider=openai_realtime FAILED pop: %s",
                    self.call_id,
                    exc,
                )
                parked = None
            if parked is None:
                logger.info(
                    "[PREWARM] callId=%s provider=openai_realtime no slot present "
                    "(cache miss / parked task still in flight)",
                    self.call_id,
                )
        parked_realtime_ws = (parked or {}).get("openai_realtime")
        adopt_ok = False
        if parked_realtime_ws is not None:
            adopt = getattr(self._adapter, "adopt_websocket", None)
            # Liveness check robust across ``websockets`` versions. The
            # legacy client exposes a ``closed`` bool, the new asyncio
            # client exposes ``state`` (websockets.protocol.State enum)
            # and ``close_code`` (None while OPEN). Pre-2025-04 we used
            # ``getattr(ws, "closed", True)`` which defaulted to True
            # when the attribute didn't exist — causing the GA-shape
            # parked WS to be treated as dead and forcibly closed
            # right before adoption.
            ws_alive = _is_parked_ws_alive(parked_realtime_ws)
            ws_closed = not ws_alive
            if not callable(adopt):
                logger.info(
                    "[PREWARM] callId=%s provider=openai_realtime adopter missing "
                    "adopt_websocket method",
                    self.call_id,
                )
            elif not ws_alive:
                logger.info(
                    "[PREWARM] callId=%s provider=openai_realtime parked WS died "
                    "between park and adopt (closed=%s)",
                    self.call_id,
                    ws_closed,
                )
            else:
                try:
                    adopt(parked_realtime_ws)
                    logger.info(
                        "[CONNECT] callId=%s provider=openai_realtime source=adopted ms=0",
                        self.call_id,
                    )
                    adopt_ok = True
                except Exception as exc:  # noqa: BLE001
                    logger.info(
                        "[PREWARM] callId=%s provider=openai_realtime adopt FAILED: %s",
                        self.call_id,
                        exc,
                    )
            if not adopt_ok:
                try:
                    await parked_realtime_ws.close()
                except Exception:
                    pass
        if not adopt_ok:
            await self._adapter.connect()
        logger.debug(
            "OpenAI Realtime connected (adapter=%s)",
            getattr(_adapter_cls, "__name__", repr(_adapter_cls)),
        )

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
                    # Filter known Whisper-on-silence hallucinations. The
                    # Realtime API's input_audio_transcription is Whisper,
                    # and Whisper's training-set bias means PSTN echo /
                    # silence segments often transcribe as
                    # "Thank you for watching." / "Thanks for watching." /
                    # "[music]" etc. — feeding those back to the LLM
                    # produces phantom user turns the caller never spoke.
                    _ev_stripped = (
                        (ev_data or "").strip().rstrip(".,!?;: ").strip().lower()
                    )
                    if _ev_stripped in _STT_HALLUCINATIONS or not _ev_stripped:
                        logger.info(
                            "Realtime transcript_input dropped (likely "
                            "Whisper hallucination on silence/echo): %r",
                            sanitize_log_value((ev_data or "")[:60]),
                        )
                        self._user_transcript_pending = False
                        continue
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
                    # Drive the assistant response. The session config sets
                    # ``turn_detection.create_response: false`` so OpenAI's
                    # server VAD no longer auto-creates a response on every
                    # ``input_audio_buffer.committed`` — that path triggers
                    # phantom assistant turns on Whisper-hallucinated input
                    # ("Thank you for watching." etc.). Patter now requests
                    # the response explicitly here, AFTER the
                    # hallucination filter accepts the transcript above.
                    request_response = getattr(self._adapter, "request_response", None)
                    if callable(request_response):
                        try:
                            await request_response()
                        except Exception as exc:  # noqa: BLE001
                            logger.debug("Realtime request_response failed: %s", exc)
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
                    # Gate the cancel/flush path with an anti-flicker window
                    # similar to the pipeline mode. OpenAI's server VAD
                    # fires ``speech_started`` on echo of the agent's own
                    # audio in PSTN no-AEC scenarios (carrier loopback
                    # feeds our outbound mulaw back into the input buffer).
                    # Without this gate every phantom ``speech_started``
                    # cancels the response — most visibly, the
                    # firstMessage gets truncated mid-sentence.
                    #
                    # ``OpenAIRealtimeStreamHandler`` doesn't carry the
                    # full pipeline TTS-tracking state (no
                    # ``_is_speaking`` / ``_first_audio_sent_at``), so
                    # we use the adapter's own response-tracking
                    # attributes as a proxy.
                    response_started_at = getattr(
                        self._adapter,
                        "_current_response_first_audio_at",
                        None,
                    )
                    if response_started_at is not None:
                        elapsed = time.monotonic() - response_started_at
                        if elapsed < MIN_AGENT_SPEAKING_S_BEFORE_BARGE_IN_NO_AEC:
                            logger.info(
                                "Realtime barge-in suppressed (response < gate, %.2fs)",
                                elapsed,
                            )
                            continue
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
                        try:
                            args = (
                                json.loads(raw_args)
                                if isinstance(raw_args, str)
                                else raw_args
                            )
                        except (json.JSONDecodeError, ValueError):
                            logger.warning(
                                "function_call transfer_call: malformed JSON args, skipping"
                            )
                            continue
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
                        try:
                            args = (
                                json.loads(raw_args)
                                if isinstance(raw_args, str)
                                else raw_args
                            )
                        except (json.JSONDecodeError, ValueError):
                            logger.warning(
                                "function_call end_call: malformed JSON args, skipping"
                            )
                            continue
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
                                try:
                                    args = json.loads(args)
                                except (json.JSONDecodeError, ValueError):
                                    logger.warning(
                                        "function_call %s: malformed JSON args, skipping",
                                        func_data["name"],
                                    )
                                    continue
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
        if self._pending_assistant_timer is not None:
            self._pending_assistant_timer.cancel()
            self._pending_assistant_timer = None
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
                        if self.on_transcript:
                            await self.on_transcript(
                                {
                                    "role": "assistant",
                                    "text": current_agent_text,
                                    "call_id": self.call_id,
                                    "history": list(self.conversation_history),
                                }
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
        pop_prewarm_audio=None,
        pop_prewarmed_connections=None,
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
        # Optional accessor returning pre-rendered first-message audio for
        # ``call_id``. Wired by ``Patter.serve()`` when the parent client
        # has ``agent.prewarm_first_message=True``. ``None`` (default) means
        # "no prewarm — always run live TTS".
        self._pop_prewarm_audio = pop_prewarm_audio
        # Optional accessor returning pre-opened, fully-handshaked
        # provider WebSockets for ``call_id``. Wired by ``Patter.serve()``.
        # Returning ``None`` means "no parked sockets — fall back to
        # fresh ``connect()``".
        self._pop_prewarmed_connections = pop_prewarmed_connections
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
        # Optional deferred STT connect task — set when prewarm-handoff
        # parallelises STT.connect with the firstMessage TTS synth.
        # Awaited BEFORE the STT receive loop starts so the message
        # pump never reads from a half-open WS.
        self._stt_connect_task: asyncio.Task | None = None
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
        # Wall-clock timestamp (``time.time()`` units) when the FIRST TTS
        # audio chunk of the current turn actually reached the carrier wire
        # — set by ``_mark_first_audio_sent`` after ``audio_sender.send_audio``
        # succeeds, cleared by ``_begin_speaking`` / barge-in cancels. The
        # barge-in gate is anchored to this timestamp instead of
        # ``_speaking_started_at`` because cloud TTS providers (ElevenLabs,
        # Cartesia, ...) take 200-700 ms to emit the first byte. A gate
        # starting at ``_begin_speaking`` would expire on background noise
        # before any audio went out, exit the TTS loop on
        # ``_is_speaking=False``, and silently drop the agent's first turn.
        self._first_audio_sent_at: float | None = None
        # Optional barge-in confirmation strategies (see
        # ``getpatter.services.barge_in_strategies``). With an empty tuple
        # the SDK uses the legacy "cancel on first VAD speech_start"
        # behaviour. With one or more strategies, a VAD speech_start during
        # TTS marks the barge-in as *pending* — the agent's TTS keeps
        # streaming naturally — and the strategies are consulted on every
        # STT transcript. The first strategy that approves confirms the
        # barge-in and the cancel/flush sequence runs; if none confirm
        # within ``_barge_in_confirm_s`` the pending state is dropped and
        # the agent finishes its sentence.
        self._barge_in_strategies: tuple = tuple(
            getattr(agent, "barge_in_strategies", ()) or ()
        )
        _confirm_ms = getattr(agent, "barge_in_confirm_ms", 1500)
        try:
            self._barge_in_confirm_s: float = max(0.1, float(_confirm_ms) / 1000.0)
        except (TypeError, ValueError):
            self._barge_in_confirm_s = 1.5
        # Wall-clock timestamp of the most recent VAD-marked pending
        # barge-in. ``None`` means "not pending"; a numeric value means
        # the agent has already produced a turn worth of audio AND VAD
        # has seen user speech, but no strategy has confirmed yet.
        self._barge_in_pending_since: float | None = None
        # Background task that fires the pending-timeout. Cancelled on
        # confirmation, on agent stop, and on call shutdown so a stale
        # pending never bleeds into the next turn.
        self._barge_in_pending_task = None
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
        # True when VAD fired ``speech_start`` during the agent's turn but
        # the barge-in gate suppressed it. The grace-timer flip drains the
        # ring buffer to STT so the user's words are not silently discarded.
        # Mirrors TS ``suppressedSpeechPending``.
        self._suppressed_speech_pending: bool = False
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
        # FIFO of outstanding Twilio marks the SDK has sent but not yet seen
        # echoed back. Used by the firstMessage paced sender to bound the
        # carrier-side buffer depth — without this the loop pushed the entire
        # TTS stream into Twilio's WebSocket in one burst and a sendClear
        # racing the queued media frames was unable to interrupt the agent
        # for up to ~2 s (BUG #128). ``on_mark`` pops entries when Twilio
        # confirms playback; ``_drain_pending_marks`` resolves every entry on
        # cancel so any awaiter exits on the next tick. Telnyx never
        # populates this queue (no mark concept on Telnyx's wire protocol —
        # the loop falls back to time-based pacing).
        self._pending_marks: list[tuple[str, asyncio.Future[None]]] = []
        # Monotonic counter for first-message mark names. Distinct from the
        # generic ``audio_*`` marks the Realtime path sends so the two paths
        # can coexist without name collisions.
        self._first_message_mark_counter: int = 0
        # Cached result of ``_is_tts_output_format_native_for_carrier()``
        # — settled once at ``start()`` time after ``set_telephony_carrier``
        # has run on the TTS adapter. ``True`` means
        # ``_encode_pipeline_audio`` can take the bypass path (raw bytes
        # → base64, no resample/encode). Parity with TS
        # ``StreamHandler.ttsOutputFormatNativeForCarrier``.
        self._tts_output_format_native_for_carrier: bool = False

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
        # Re-evaluate after set_telephony_carrier so the _encode_pipeline_audio
        # fast path is enabled for the current carrier when the adapter
        # auto-flipped (or the user constructed with a native format).
        # Parity with TS ``StreamHandler.ttsOutputFormatNativeForCarrier``.
        self._tts_output_format_native_for_carrier = (
            self._is_tts_output_format_native_for_carrier()
        )
        if self._tts_output_format_native_for_carrier:
            logger.debug(
                "TTS outputFormat matches %s wire codec — bypassing client-side transcode",
                "twilio" if self._for_twilio else "telnyx",
            )
            # Flip the audio sender into pass-through mode so it stops
            # transcoding (16 kHz PCM → mulaw) bytes that are already in
            # the carrier's wire format. Mirrors the ConvAI handler's
            # ``_native_mulaw_8k`` fast-path and TS ``encodePipelineAudio``
            # bypass. Parity with TS ``StreamHandler.ttsOutputFormatNativeForCarrier``.
            if hasattr(self.audio_sender, "_input_is_mulaw_8k"):
                self.audio_sender._input_is_mulaw_8k = True  # type: ignore[attr-defined]

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
        # Per the industry consensus on PSTN echo cancellation and
        # Twilio's own guidance, time-domain NLMS server-side
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

        # Prewarm-handoff: try to adopt pre-opened provider WebSockets
        # that the prewarm pipeline (see
        # ``Patter._park_provider_connections``) parked during the
        # carrier ringing window. When a parked WS is still OPEN we
        # skip the cold ``connect()`` and the STT first-turn can flow
        # audio without paying the 150-400 ms TLS handshake. Failures
        # (cache miss, parked WS died) fall back transparently.
        parked: dict | None = None
        if self._pop_prewarmed_connections is not None:
            try:
                parked = self._pop_prewarmed_connections(self.call_id)
            except Exception as exc:  # noqa: BLE001 - best-effort
                logger.debug("pop_prewarmed_connections raised: %s", exc)
                parked = None

        # Adopt the TTS WS first — synchronous handoff (the live
        # ``synthesize`` call below picks it up via the adapter's
        # single-slot adoption queue).
        parked_tts = (parked or {}).get("tts")
        if parked_tts is not None and self._tts is not None:
            adopt = getattr(self._tts, "adopt_websocket", None)
            ws_alive = parked_tts.ws is not None and _is_parked_ws_alive(parked_tts.ws)
            if callable(adopt) and ws_alive:
                try:
                    adopt(parked_tts)
                    logger.info(
                        "[CONNECT] callId=%s provider=tts source=adopted ms=0",
                        self.call_id,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("TTS adopt_websocket failed: %s; falling back", exc)
                    try:
                        await parked_tts.ws.close()
                    except Exception:
                        pass
            else:
                try:
                    await parked_tts.ws.close()
                except Exception:
                    pass

        # Kick off STT connect WITHOUT awaiting yet — we only need STT
        # ready to receive incoming user audio, not to send the first
        # agent message out. Parallelising STT.connect with the TTS
        # firstMessage synth shaves 200-400 ms off the perceived
        # first-turn latency.
        stt_connect_task: asyncio.Task | None = None
        if self._stt is not None:
            parked_stt = (parked or {}).get("stt")
            adopt_stt = getattr(self._stt, "adopt_websocket", None)
            stt_started_at = time.monotonic()
            stt_adopted = False
            if (
                parked_stt is not None
                and callable(adopt_stt)
                and isinstance(parked_stt, tuple)
                and len(parked_stt) == 2
            ):
                session, ws = parked_stt
                if _is_parked_ws_alive(ws):
                    try:
                        adopt_stt(session, ws)
                        logger.info(
                            "[CONNECT] callId=%s provider=stt source=adopted ms=%d",
                            self.call_id,
                            int((time.monotonic() - stt_started_at) * 1000),
                        )
                        stt_adopted = True
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(
                            "STT adopt_websocket failed: %s; falling back", exc
                        )
                        try:
                            await ws.close()
                        except Exception:
                            pass
                        try:
                            await session.close()
                        except Exception:
                            pass
                else:
                    try:
                        await ws.close()
                    except Exception:
                        pass
                    try:
                        await session.close()
                    except Exception:
                        pass
            elif parked_stt is not None:
                # Unknown handle shape — discard cleanly.
                await _safe_close_parked_handle(parked_stt)

            if not stt_adopted:

                async def _connect_stt() -> None:
                    await self._stt.connect()
                    logger.info(
                        "[CONNECT] callId=%s provider=stt source=fresh ms=%d",
                        self.call_id,
                        int((time.monotonic() - stt_started_at) * 1000),
                    )

                stt_connect_task = asyncio.create_task(_connect_stt())

        # Stash the deferred connect task so the receive-loop launcher
        # below awaits it before starting the message pump.
        self._stt_connect_task = stt_connect_task

        logger.debug("Pipeline mode: STT connect kicked off")

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
            #
            # ``is_first_message=True`` pre-stamps ``_first_audio_sent_at``
            # synchronously so the barge-in gate runs in parallel with TTS
            # TTFB instead of only after audio arrives — without this, the
            # firstMessage is effectively un-interruptible for 300-800 ms.
            await self._begin_speaking(is_first_message=True)
            first_chunk_sent = False
            # Drop any stale PCM16 carry byte from a prior synth (none at call
            # start, but defensive for parity with TS ``ttsByteCarry = null``).
            self.audio_sender.reset_pcm_carry()
            # Check the prewarm cache first. When ``Patter.call`` was made
            # with ``agent.prewarm_first_message=True`` the firstMessage
            # has already been synthesised during the ringing window — we
            # stream the bytes directly through the carrier-side
            # AudioSender (which handles native-rate → carrier-rate
            # resampling) and skip the TTS round-trip entirely.
            prewarm_bytes: bytes | None = None
            if self._pop_prewarm_audio is not None:
                try:
                    prewarm_bytes = self._pop_prewarm_audio(self.call_id)
                except Exception as exc:  # noqa: BLE001 - best-effort
                    logger.debug("pop_prewarm_audio raised: %s", exc)
                    prewarm_bytes = None
            try:
                if prewarm_bytes:
                    if self.metrics is not None:
                        self.metrics.record_tts_first_byte()
                    first_chunk_sent = await self._stream_prewarm_bytes(prewarm_bytes)
                else:
                    # Streaming TTS path (no prewarm cache). Uses the same
                    # simple per-chunk send as _synthesize_sentence —
                    # ElevenLabs HTTP streams at near-real-time speed so the
                    # carrier-side buffer stays bounded without mark-gated
                    # pacing.  Routing streaming chunks through
                    # _send_paced_first_message_bytes caused crackling: its
                    # drain+reset on every HTTP chunk destroyed mark
                    # back-pressure continuity and the per-sub-chunk sleep
                    # slowed delivery below Twilio's playout rate, producing
                    # periodic buffer underruns.  The prewarm path (a single
                    # pre-synthesised buffer) still uses
                    # _send_paced_first_message_bytes because that buffer can
                    # be several seconds long and needs pacing.
                    async for audio_chunk in self._tts.synthesize(
                        self.agent.first_message
                    ):
                        if not self._is_speaking:
                            break
                        if not first_chunk_sent:
                            first_chunk_sent = True
                            if self.metrics is not None:
                                self.metrics.record_tts_first_byte()
                        if self._aec is not None:
                            self._aec.push_far_end(audio_chunk)
                        await self.audio_sender.send_audio(audio_chunk)
                        self._mark_first_audio_sent()
            finally:
                # Drop any partial int16 byte to prevent cross-turn corruption
                # if the stream threw before a complete sample was delivered.
                self.audio_sender.reset_pcm_carry()
                # Flip back to not-speaking with grace so the ring
                # buffer accumulated during the intro is flushed and
                # the next user utterance is recognised cleanly.
                await self._end_speaking_with_grace()
            if first_chunk_sent and self.metrics is not None:
                # Bill the firstMessage TTS characters — they were synthesised
                # at ElevenLabs (or the configured TTS provider) and the
                # customer pays for them. The previous flow only called
                # ``record_turn_complete`` here, which finalises the turn
                # but does NOT increment ``_total_tts_characters`` — so a
                # 5-turn call with an 82-char greeting was under-billed
                # by ~22% on TTS cost. ``record_tts_complete`` is the
                # canonical accumulator entry point for TTS char billing.
                self.metrics.record_tts_complete(self.agent.first_message)
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

            # Inject the built-in transfer_call / end_call tools — parity with
            # the realtime path (see ``OpenAIRealtimeStreamHandler.start``
            # where ``openai_tools = agent_tools + [TRANSFER_CALL_TOOL,
            # END_CALL_TOOL]``). Without this, pipeline-mode LLMs never see
            # the built-ins and can't initiate a handoff or hangup no matter
            # what the system prompt says.
            # Merge the built-in consult tool (if configured) before the
            # handoff built-ins so the pipeline LLM sees it too.
            self.agent = _inject_consult_tool(self.agent)
            combined_tools = _augment_with_builtin_handoff_tools(
                self.agent.tools,
                transfer_fn=self._transfer_fn,
                hangup_fn=self._hangup_fn,
            )
            tool_executor = ToolExecutor() if combined_tools else None
            llm_model = self.agent.model
            if "realtime" in llm_model:
                llm_model = "gpt-4o-mini"
            self._llm_loop = LLMLoop(
                openai_key=self._openai_key,
                model=llm_model,
                system_prompt=self.resolved_prompt,
                tools=combined_tools,
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

        # Start STT receive loop. If we kicked off the WS connect in
        # parallel with the firstMessage TTS, make sure that connect
        # has completed before the receive loop starts polling — a
        # half-open WS would surface "Not connected. Call connect()
        # first." on the first audio frame.
        if self._stt_connect_task is not None:
            try:
                await self._stt_connect_task
            except Exception as exc:  # noqa: BLE001
                logger.error("STT connect failed: %s", exc)
                # Tear down the call cleanly — we can't proceed with
                # transcription. The carrier-side pump will see the
                # closed WS and end the call.
                if self._hangup_fn is not None:
                    try:
                        await self._hangup_fn(self.call_id)
                    except Exception:
                        pass
                return
            finally:
                self._stt_connect_task = None
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
                self._mark_first_audio_sent()
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
        """Decide whether ``transcript`` confirms a barge-in and run the
        cancel/flush path if so. Mirrors TS ``handleBargeIn``.

        The legacy contract — "any transcript while speaking cancels the
        agent" — applies when ``agent.barge_in_strategies`` is empty.
        With one or more strategies configured, the transcript is fed
        to :func:`evaluate_strategies` and the cancel only runs when at
        least one strategy approves; otherwise the agent keeps talking.
        """
        if not (transcript.text and self._is_speaking):
            return
        if not self._can_barge_in():
            aec_state = "on" if getattr(self, "_aec", None) is not None else "off"
            logger.info(
                "Barge-in transcript suppressed (agent speaking < gate, aec=%s)",
                aec_state,
            )
            return
        strategies = getattr(self, "_barge_in_strategies", ()) or ()
        if strategies:
            from getpatter.services.barge_in_strategies import evaluate_strategies

            confirmed = await evaluate_strategies(
                strategies,
                transcript=transcript.text,
                is_interim=not getattr(transcript, "is_final", True),
                agent_speaking=self._is_speaking,
            )
            if not confirmed:
                logger.debug(
                    "Barge-in NOT confirmed by any strategy (transcript=%r); "
                    "agent continues talking",
                    sanitize_log_value(transcript.text[:40]),
                )
                return
            logger.info(
                "Barge-in confirmed by strategy on transcript %r",
                sanitize_log_value(transcript.text[:40]),
            )
        await self._do_cancel_for_barge_in(transcript.text)

    async def _do_cancel_for_barge_in(self, transcript_text: str) -> None:
        """Actually cancel the in-flight agent turn (TTS + LLM stream + ring).

        Split out of :meth:`_handle_barge_in` so the same cancel logic can
        run from the legacy "transcript = cancel" path AND the opt-in
        "strategy confirmed = cancel" path without duplication.
        """
        # Capture pending state BEFORE _clear_pending_barge_in() drops it —
        # if VAD already started the overlap window via
        # ``_start_pending_barge_in`` we MUST NOT call ``record_overlap_start``
        # again (that would overwrite T1 with T2 and produce a near-zero
        # ``InterruptionMetrics.detection_delay_ms`` on the strategy path).
        # ``getattr`` is defensive against test fixtures that build a
        # handler shell via ``object.__new__`` and don't initialise the
        # pending-barge-in state — the safe default is "no pending".
        had_pending = getattr(self, "_barge_in_pending_since", None) is not None
        self._clear_pending_barge_in()
        if self.metrics is not None:
            if not had_pending:
                # Legacy path or VAD never fired — start the overlap window now.
                self.metrics.record_overlap_start()
            self.metrics.record_bargein_detected()
        logger.debug(
            "Barge-in: caller spoke over agent (%s)",
            sanitize_log_value(transcript_text[:40]),
        )
        with start_span(
            SPAN_BARGEIN,
            {"patter.call.id": self.call_id},
        ):
            self._is_speaking = False
            self._speaking_started_at = None
            self._first_audio_sent_at = None
            self._last_cancel_at = time.time()
            # Unblock any firstMessage paced-send loop that's sitting in
            # ``_wait_for_mark_window`` — without this the loop keeps
            # awaiting echoes for up to ``_MARK_AWAIT_TIMEOUT_S`` per
            # outstanding mark before observing ``_is_speaking=False``,
            # which keeps the agent "speaking" from the user's perspective
            # for hundreds of extra ms after barge-in (BUG #128). Defensive
            # ``getattr`` is for test fixtures that build a handler shell
            # via ``object.__new__`` and skip ``__init__``.
            if getattr(self, "_pending_marks", None) is not None:
                self._drain_pending_marks()
            cancel_event = getattr(self, "_llm_cancel_event", None)
            if cancel_event is not None:
                cancel_event.set()
            # Force-close any in-flight TTS streaming socket. Without this,
            # the firstMessage live ``synthesize`` path (used when the prewarm
            # accumulator hadn't completed before pickup) would block on its
            # inner ``await ws.recv()`` for up to ``frame_timeout`` (30 s) —
            # ``_init_pipeline`` would never return, the STT ``on_transcript``
            # callback would never register, and every subsequent user turn
            # would be silently dropped. Provider-duck-typed: adapters that
            # don't expose ``cancel_active_stream`` are no-ops here.
            # Parity with TS ``StreamHandler.cancelSpeaking``.
            _tts = getattr(self, "_tts", None)
            _cancel_fn = getattr(_tts, "cancel_active_stream", None)
            if callable(_cancel_fn):
                try:
                    _cancel_fn()
                except Exception as _exc:
                    logger.debug("TTS cancel_active_stream raised: %s", _exc)
            try:
                await self.audio_sender.send_clear()
            except Exception as exc:
                logger.debug("send_clear during barge-in failed: %s", exc)
            if self.metrics is not None:
                self.metrics.record_tts_stopped()
                self.metrics.record_turn_interrupted()
                # Re-anchor to legitimate VAD speech_start so post-barge-in
                # latency anchors don't carry from the interrupted turn.
                self.metrics.anchor_user_speech_start()
                self.metrics.record_overlap_end(was_interruption=True)

    async def _start_pending_barge_in(self) -> None:
        """Mark a VAD-detected barge-in as pending (no cancel yet).

        Only used when ``agent.barge_in_strategies`` is non-empty. The
        agent's TTS keeps streaming naturally; an
        :meth:`_pending_barge_in_timeout` task will drop the pending
        state if no strategy confirms within ``_barge_in_confirm_s``.
        """
        if self._barge_in_pending_since is not None:
            return
        self._barge_in_pending_since = time.time()
        if self.metrics is not None:
            self.metrics.record_overlap_start()
        logger.info(
            "Barge-in PENDING (VAD speech_start during TTS); awaiting strategy confirmation"
        )
        try:
            self._barge_in_pending_task = asyncio.create_task(
                self._pending_barge_in_timeout()
            )
        except RuntimeError as exc:  # pragma: no cover - no running loop
            logger.debug("could not schedule pending barge-in timeout: %s", exc)
            self._barge_in_pending_task = None

    async def _pending_barge_in_timeout(self) -> None:
        try:
            await asyncio.sleep(self._barge_in_confirm_s)
        except asyncio.CancelledError:
            return
        if self._barge_in_pending_since is None:
            return
        logger.info(
            "Pending barge-in timed out after %.2fs; agent resumes (no strategy confirmed)",
            self._barge_in_confirm_s,
        )
        if self.metrics is not None:
            self.metrics.record_overlap_end(was_interruption=False)
            # Re-anchor to legitimate VAD speech_start so anchors that drifted
            # during the pending barge-in window don't pollute the next turn.
            self.metrics.anchor_user_speech_start()
        self._barge_in_pending_since = None
        self._barge_in_pending_task = None

    def _clear_pending_barge_in(self) -> None:
        """Drop pending state without cancelling — used on confirm and on
        agent stop. Idempotent and safe to call from test fixtures that
        construct the handler via ``object.__new__`` (no __init__)."""
        task = getattr(self, "_barge_in_pending_task", None)
        if task is not None and not task.done():
            task.cancel()
        self._barge_in_pending_task = None
        self._barge_in_pending_since = None

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
                    # Final transcript dropped (dedup / hallucination /
                    # back-to-back). Any VAD ``speech_end`` that fired
                    # during this dropped utterance already stamped
                    # ``_endpoint_signal_at``; if we leave it there, the
                    # NEXT legitimate utterance inherits the stale anchor
                    # (its agent_response_ms then includes the silence
                    # gap between the dropped utterance and the real one).
                    if self.metrics is not None:
                        self.metrics.anchor_user_speech_start()
                    continue

                await self._dispatch_turn(transcript.text)

        except Exception as exc:
            logger.exception("Pipeline STT loop error: %s", exc)

    async def _dispatch_turn(self, transcript_text: str) -> None:
        """Run the post-commit pipeline (record STT → afterTranscribe →
        LLM dispatch → TTS → turn-complete) inline on the STT loop.
        """
        # Record one STT span per final transcript turn. The span is
        # short-lived (just the attribute set) because STT is
        # streaming — we do not re-wrap the long-lived iterator.
        with start_span(
            SPAN_STT,
            {
                "getpatter.stt.text_len": len(transcript_text),
                "patter.call.id": self.call_id,
            },
        ):
            pass

        logger.debug("User: %s", sanitize_log_value(transcript_text))

        if self.metrics is not None:
            self.metrics.start_turn_if_idle()  # turn may already be open
            # Known limitation: per-turn audio_seconds is not tracked
            # here; metrics rely on total _stt_byte_count plus the
            # end_call() estimation pass.
            self.metrics.record_vad_stop()
            self.metrics.record_stt_complete(transcript_text)
            self.metrics.record_stt_final_timestamp()

        # Endpoint span — silence-detected → LLM-dispatch window. Open
        # here (right after VAD stop / final transcript is recorded)
        # and close it just before ``record_turn_committed`` below.
        endpoint_span = start_span(
            SPAN_ENDPOINT,
            {"patter.call.id": self.call_id},
        )
        endpoint_span.__enter__()
        endpoint_closed = False

        def _close_endpoint_span() -> None:
            nonlocal endpoint_closed
            if endpoint_closed:
                return
            endpoint_closed = True
            try:
                endpoint_span.__exit__(None, None, None)
            except Exception:  # pragma: no cover - defensive
                pass

        # Raw transcript always goes to dashboard/transcript log
        self.transcript_entries.append({"role": "user", "text": transcript_text})

        # Reuse the timestamp already captured by _commit_transcript and stored
        # in self._last_commit_at. This avoids a second time.time() call per
        # transcript, which would exhaust the finite fake-clock iterators used
        # in unit tests (and is wasteful in production too).
        _turn_ts = self._last_commit_at

        # Append raw text to conversation_history NOW so that on_transcript
        # receives a history snapshot that includes the current user turn
        # (parity with OpenAIRealtimeStreamHandler which appends before firing
        # on_transcript). Replaced by filtered_text below, or popped on any
        # early-return path so a vetoed/orphaned turn never lingers.
        self.conversation_history.append(
            {"role": "user", "text": transcript_text, "timestamp": _turn_ts}
        )

        if self.on_transcript:
            await self.on_transcript(
                {
                    "role": "user",
                    "text": transcript_text,
                    "call_id": self.call_id,
                    "history": list(self.conversation_history),
                }
            )

        # --- afterTranscribe hook ---
        hooks = getattr(self.agent, "hooks", None)
        hook_executor = PipelineHookExecutor(hooks)
        hook_ctx = self._build_hook_context()
        filtered_text = await hook_executor.run_after_transcribe(
            transcript_text, hook_ctx
        )
        if filtered_text is None:
            logger.debug("afterTranscribe hook vetoed turn")
            if self.metrics is not None:
                self.metrics.record_turn_interrupted()
            # Remove the speculatively-appended user turn before returning so a
            # vetoed turn does not linger in conversation_history.
            if (
                self.conversation_history
                and self.conversation_history[-1].get("text") == transcript_text
            ):
                self.conversation_history.pop()
            _close_endpoint_span()
            return

        if self.metrics is not None:
            self.metrics.record_on_user_turn_completed_delay(0.0)
        if self.on_message is None and self._llm_loop is None:
            # No message handler or LLM loop — discard orphaned turn.
            if self.metrics is not None:
                self.metrics.record_turn_interrupted()
            # Pop the speculatively-appended user turn so it does not
            # accumulate as an orphaned entry when there is nothing to consume
            # it (no handler and no built-in LLM loop).
            if (
                self.conversation_history
                and self.conversation_history[-1].get("text") == transcript_text
            ):
                self.conversation_history.pop()
            _close_endpoint_span()
            return

        # Replace the raw-text speculative entry with filtered_text (the text
        # actually sent to the LLM).
        if (
            self.conversation_history
            and self.conversation_history[-1].get("text") == transcript_text
        ):
            self.conversation_history.pop()
        self.conversation_history.append(
            {"role": "user", "text": filtered_text, "timestamp": _turn_ts}
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
            response_text = await self._process_streaming_response(result, self.call_id)
            if response_text:
                await self._emit_assistant_transcript(response_text)
            return

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
                response_text = await remote.call_webhook(self.on_message, msg_data)
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
            response_text = await self._process_streaming_response(result, self.call_id)
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
                    phantom_suppressed = self._is_speaking and not self._can_barge_in()
                    if phantom_suppressed:
                        # Within the per-turn warmup gate. With AEC on
                        # this is the ~1 s filter convergence window;
                        # without AEC it is just a 0.25 s anti-flicker
                        # margin. INFO so unexpected suppressions are
                        # visible without enabling debug logs.
                        #
                        # CRITICAL: do NOT touch metrics state here.
                        # An earlier bug (pre-0.6.1) called
                        # ``start_turn_if_idle()`` for every
                        # ``speech_start`` including suppressed phantoms,
                        # which stamped ``_turn_start`` at echo/loopback
                        # time. ``start_turn_if_idle`` then no-op'd on
                        # the legitimate user-speech ``speech_start``
                        # that followed (turn_start was already set),
                        # so ``user_speech_duration_ms`` was reported as
                        # 5-7 s even on short ~1 s utterances.
                        aec_state = (
                            "on" if getattr(self, "_aec", None) is not None else "off"
                        )
                        logger.info(
                            "VAD speech_start suppressed (agent speaking < gate, aec=%s)",
                            aec_state,
                        )
                        # Real user speech detected but gated out. The
                        # grace-timer flip will drain the ring buffer to
                        # STT so the user's words are not silently lost.
                        self._suppressed_speech_pending = True
                    elif self._is_speaking:
                        # Caller spoke over in-flight TTS. With opt-in
                        # confirmation strategies the cancel is deferred
                        # until at least one strategy approves the user's
                        # transcript; otherwise we keep the legacy
                        # "cancel immediately" path so existing users
                        # see no behaviour change.
                        if self._barge_in_strategies:
                            await self._start_pending_barge_in()
                        else:
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
                                        "send_clear during VAD barge-in failed: %s",
                                        exc,
                                    )
                                await self._flush_inbound_audio_ring()
                                if self.metrics is not None:
                                    self.metrics.record_tts_stopped()
                                    self.metrics.record_turn_interrupted()
                                self._is_speaking = False
                                self._speaking_started_at = None
                                self._first_audio_sent_at = None
                                self._speaking_generation += 1
                                self._last_cancel_at = time.time()
                                self._suppressed_speech_pending = False
                    if not phantom_suppressed and self.metrics is not None:
                        # Industry-standard pattern: every legitimate VAD speech_start
                        # re-anchors the turn timestamp pre-commit. This
                        # repairs the case where a partial transcript /
                        # rejected barge-in already stamped stale anchors,
                        # plus the original "phantom during warmup gate"
                        # vulnerability. No-op once the turn is committed.
                        self.metrics.anchor_user_speech_start()
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

    async def _begin_speaking(self, is_first_message: bool = False) -> None:
        """Mark TTS playback as in-progress and bump the generation counter.

        Awaits the post-cancel drain window before flipping state so the
        remote PSTN player has time to flush the cancelled turn's tail.

        The generation counter is consulted by ``_end_speaking_with_grace``
        so a delayed flip-to-idle from a previous turn cannot cancel the
        speaking flag of the *current* turn.

        Args:
            is_first_message: When ``True`` stamps ``_first_audio_sent_at``
                synchronously before the TTS loop starts so the
                ``_can_barge_in()`` 250 ms anti-flicker gate (no-AEC PSTN
                default) runs in PARALLEL with TTS TTFB rather than only
                starting after audio actually arrives. Without this, the
                firstMessage is effectively un-interruptible for the first
                300-800 ms while waiting on cloud TTS first-byte.
        """
        if self._last_cancel_at is not None:
            elapsed = time.time() - self._last_cancel_at
            remaining = self._POST_CANCEL_DRAIN_S - elapsed
            if remaining > 0:
                await asyncio.sleep(remaining)
        self._speaking_generation += 1
        self._is_speaking = True
        self._speaking_started_at = time.time()
        # Stamp ``_first_audio_sent_at`` synchronously for EVERY turn so the
        # ``_can_barge_in()`` gate (250 ms anti-flicker for PSTN no-AEC) runs
        # in PARALLEL with LLM TTFT + TTS TTFB rather than starting only
        # after the first audio chunk reaches the wire. Without this, a turn
        # with a slow LLM (gpt-4o cold cache ~2 s) is effectively
        # un-interruptible for the entire LLM window: ``_first_audio_sent_at``
        # stays None, ``_can_barge_in`` returns False, and every VAD
        # ``speech_start`` is suppressed silently. Promoted from
        # firstMessage-only to default on 2026-05-14 (TS parity).
        # ``is_first_message`` is kept for backward compat with callers but
        # no longer changes behaviour.
        _ = is_first_message
        self._first_audio_sent_at = time.time()
        # Fresh turn — drop any stale pre-barge-in buffer from a previous
        # turn so we never replay yesterday's audio to STT.
        self._inbound_audio_ring = []
        self._suppressed_speech_pending = False
        # Reset the VAD detector so the next user utterance triggers a clean
        # SILENCE→SPEECH transition. Without this, PSTN echo from the
        # previous turn can keep the smoothed probability above the
        # deactivation threshold (0.35) for the entire turn — the VAD never
        # returns to SILENCE, ``speech_start`` never fires, and barge-in
        # feels "one-shot". The user's previous utterance was already
        # committed by STT before ``_begin_speaking`` is called, so resetting
        # state here cannot lose data.
        self._reset_vad()

    def _mark_first_audio_sent(self) -> None:
        """Record that the first TTS chunk of the current turn hit the wire.

        Idempotent within a turn: only the first call sets the timestamp.
        Must be invoked AFTER the underlying ``audio_sender.send_audio`` so
        the gate is anchored to "audio actually went out", not "we asked
        the carrier to send it". Mirrors TS ``markFirstAudioSent``.
        """
        if self._first_audio_sent_at is None:
            self._first_audio_sent_at = time.time()

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
        # Anchor the gate on "first audio actually emitted", not on
        # ``_begin_speaking`` (which fires before the TTS provider's
        # first-byte latency has elapsed). Without this guard, background
        # noise picked up by VAD ~250 ms after ``_begin_speaking`` triggers
        # a self-cancel BEFORE any TTS chunk has reached the wire — the
        # agent's first turn becomes silence even though the SDK believes
        # it spoke. Mirrors TS ``canBargeIn``.
        first_audio_at = getattr(self, "_first_audio_sent_at", None)
        if first_audio_at is None:
            return False
        elapsed = time.time() - first_audio_at
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
            self._first_audio_sent_at = None
            self._clear_pending_barge_in()
            await self._reset_barge_in_strategies()
            if self._suppressed_speech_pending:
                self._suppressed_speech_pending = False
                await self._flush_inbound_audio_ring()
            self._reset_vad()
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
                    self._first_audio_sent_at = None
                    self._clear_pending_barge_in()
                    await self._reset_barge_in_strategies()
                    if self._suppressed_speech_pending:
                        self._suppressed_speech_pending = False
                        await self._flush_inbound_audio_ring()
                    # Reset VAD so any stuck SPEECH state from echo /
                    # loopback during the agent's turn does not block the
                    # next user utterance from emitting ``speech_start``.
                    self._reset_vad()
            except asyncio.CancelledError:  # pragma: no cover
                raise
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("tts grace flip failed: %s", exc)

        asyncio.create_task(_flip_after_grace())

    async def _reset_barge_in_strategies(self) -> None:
        if not self._barge_in_strategies:
            return
        from getpatter.services.barge_in_strategies import reset_strategies

        await reset_strategies(self._barge_in_strategies)

    def _reset_vad(self) -> None:
        """Reset the active VAD provider's per-utterance state.

        No-op when the provider does not implement the optional
        :py:meth:`getpatter.providers.base.VADProvider.reset` hook
        (default implementation in ``VADProvider`` is a no-op). Safe to
        call from any context — failures are swallowed; a flaky reset
        must never silently kill barge-in for every subsequent turn.

        Parity with TS ``resetVad``.
        """
        vad = getattr(self.agent, "vad", None) or self._auto_vad
        if vad is None:
            return
        try:
            vad.reset()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("VAD reset threw: %s", exc)

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

    def _is_tts_output_format_native_for_carrier(self) -> bool:
        """Return True when the TTS adapter's output_format is already in the
        carrier's wire codec — meaning no client-side resample/transcode is
        needed in ``TwilioAudioSender.send_audio``.

        Twilio expects ``ulaw_8000``; Telnyx expects ``pcm_16000``. Anything
        else goes through the normal resample-and-encode path.

        Parity with TS ``StreamHandler.isTtsOutputFormatNativeForCarrier``.
        """
        if self._tts is None:
            return False
        fmt = getattr(self._tts, "output_format", None)
        if not isinstance(fmt, str):
            return False
        carrier = "twilio" if self._for_twilio else "telnyx"
        if carrier == "twilio":
            return fmt == "ulaw_8000"
        if carrier == "telnyx":
            return fmt == "pcm_16000"
        return False

    # 40 ms @ 16 kHz mono PCM16 = 1280 bytes. Sized to mirror the smallest
    # live-TTS chunk boundary so cancel granularity (mark/clear bookkeeping)
    # is identical regardless of whether the firstMessage came from the
    # prewarm cache or a live ``tts.synthesize`` stream.
    _PREWARM_CHUNK_BYTES: int = 1280
    # Maximum unconfirmed Twilio marks while streaming firstMessage. Each
    # chunk is 40 ms of audio at 16 kHz PCM16, so a window of 3 caps the
    # in-flight queue at ~120 ms. This means a barge-in's ``send_clear`` has
    # at most ~120 ms of buffered audio to flush — vs. ~2-5 s with the
    # previous burst-send code (BUG #128). 3 hit the smallest barge-in cap
    # without audible playback gaps under typical PSTN RTT in 2026-05
    # acceptance.
    _FIRST_MESSAGE_MARK_WINDOW: int = 3
    # Per-chunk soft timeout (s) for awaiting a mark echo. Caps the
    # deadlock window when a carrier (or a test double) never echoes —
    # playout may glitch by one chunk on timeout but the call stays alive.
    _MARK_AWAIT_TIMEOUT_S: float = 0.5
    # Bytes-per-millisecond for a 16 kHz PCM16 mono stream. Used by
    # ``_send_paced_first_message_bytes`` to translate chunk size into a
    # playout-duration sleep so we never deliver faster than the carrier
    # can decode + play out (which manifested as severe crackling on the
    # HTTP-TTS path with client-side resampling). 16000 samples/sec × 2
    # bytes/sample = 32 bytes/ms.
    _PCM16_16K_BYTES_PER_MS: int = 32

    def _drain_pending_marks(self) -> None:
        """Resolve every entry in ``_pending_marks`` and empty the FIFO.

        Idempotent — safe to call from the barge-in cancel path and again
        from the grace flip without leaking unresolved futures.
        """
        if not self._pending_marks:
            return
        for _name, fut in self._pending_marks:
            if not fut.done():
                try:
                    fut.set_result(None)
                except asyncio.InvalidStateError:
                    pass
        self._pending_marks.clear()

    async def _send_mark_awaitable(self) -> asyncio.Future | None:
        """Send a Twilio ``mark`` event and return a future that resolves
        when the carrier echoes it back (via :meth:`on_mark`), or when
        :meth:`_drain_pending_marks` runs. Returns ``None`` on non-Twilio
        carriers — the caller should fall back to time-based pacing.
        """
        if not self._for_twilio:
            return None
        self._first_message_mark_counter += 1
        mark_name = f"fm_{self._first_message_mark_counter}"
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[None] = loop.create_future()
        self._pending_marks.append((mark_name, fut))
        try:
            await self.audio_sender.send_mark(mark_name)
        except Exception as exc:  # noqa: BLE001 - best effort
            logger.debug("send_mark failed (%s): %s", mark_name, exc)
            # Drop the waiter so the queue can't fill with orphans.
            for idx, (name, f) in enumerate(self._pending_marks):
                if name == mark_name:
                    self._pending_marks.pop(idx)
                    break
            if not fut.done():
                fut.set_result(None)
        return fut

    async def _wait_for_mark_window(self) -> None:
        """Block until the in-flight mark queue depth is below
        ``_FIRST_MESSAGE_MARK_WINDOW``. Returns immediately on cancel
        because :meth:`_drain_pending_marks` resolves every pending future.
        """
        while (
            self._is_speaking
            and len(self._pending_marks) >= self._FIRST_MESSAGE_MARK_WINDOW
        ):
            _name, oldest = self._pending_marks[0]
            try:
                await asyncio.wait_for(
                    asyncio.shield(oldest),
                    timeout=self._MARK_AWAIT_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                # Drop the head so subsequent loops don't deadlock on the
                # same mark forever. Twilio mark echo may have been lost
                # in transit; carrier playback will continue regardless.
                pass
            # Pop the head if still present (a successful echo would have
            # done it via ``on_mark``; only a timeout leaves it in place).
            if self._pending_marks and self._pending_marks[0][0] == _name:
                self._pending_marks.pop(0)

    async def on_mark(self, mark_name: str) -> None:
        """Handle a Twilio ``mark`` echo and resolve the matching firstMessage
        waiter (if any). Marks are matched FIFO: an echo for ``fm_3`` also
        resolves ``fm_1`` and ``fm_2`` in case the carrier batches echoes.
        """
        if not mark_name:
            return
        idx = -1
        for i, (name, _fut) in enumerate(self._pending_marks):
            if name == mark_name:
                idx = i
                break
        if idx < 0:
            return
        resolved = self._pending_marks[: idx + 1]
        del self._pending_marks[: idx + 1]
        for _name, fut in resolved:
            if not fut.done():
                try:
                    fut.set_result(None)
                except asyncio.InvalidStateError:
                    pass

    async def _stream_prewarm_bytes(self, prewarm_bytes: bytes) -> bool:
        """Stream a cached firstMessage buffer in pacing-friendly chunks."""
        return await self._send_paced_first_message_bytes(prewarm_bytes)

    async def _send_paced_first_message_bytes(self, bytes_: bytes) -> bool:
        """Iterate ``bytes_`` as ``_PREWARM_CHUNK_BYTES``-sized PCM16 slices
        and forward each via ``audio_sender.send_audio`` with mark-gated
        pacing (Twilio) or playout-time-based pacing (Telnyx).

        Caps the carrier-side buffer at ``_FIRST_MESSAGE_MARK_WINDOW``
        chunks so a barge-in's ``send_clear`` has at most ~120 ms (Twilio)
        or zero (Telnyx, immediately after the latest sleep) of audio to
        flush. The previous burst-send code let Twilio's buffer reach
        several seconds — a barge-in's ``send_clear`` race-lost against
        the queued media frames and the agent kept talking on the user's
        earpiece for up to ~2 s after the user spoke (BUG #128).

        Bails immediately when ``_is_speaking`` flips to ``False`` — both
        via the loop's pre-iter check and via :meth:`_drain_pending_marks`
        (called from the barge-in cancel path) which unblocks any
        in-flight :meth:`_wait_for_mark_window` await.

        Returns ``True`` when at least one chunk hit the wire — the caller
        uses that to decide whether to record the TTS-first-byte /
        turn-complete metrics.
        """
        # Reset the per-send mark counter so each invocation produces a
        # fresh ``fm_1, fm_2, ...`` sequence. Without this the counter
        # grows monotonically across turns on a re-used handler and a
        # stale ``fm_N`` echo from an earlier turn could match a mark
        # name issued later, corrupting the FIFO matching in
        # ``on_mark``. The ``_pending_marks`` queue is also expected
        # empty here by the caller's cancel / cleanup paths; if it is
        # not (defensive re-entry) we drain before resetting.
        if self._pending_marks:
            self._drain_pending_marks()
        self._first_message_mark_counter = 0
        first_chunk_sent = False
        # Once the mark window is first filled we switch to playout-time
        # pacing to prevent batch-ACK bursts from draining the carrier
        # jitter buffer. Before that we send in burst so the first
        # ``_FIRST_MESSAGE_MARK_WINDOW`` chunks pre-fill the PSTN jitter
        # buffer (250–1500 ms). The earlier experiment of pure-burst
        # delivery (no per-chunk sleep) produced severe carrier-side
        # crackling on the HTTP TTS path (pcm_16000 → mulaw_8000 client-
        # side resample) because the burst arrived at Twilio faster than
        # its media-stream decoder could process — even though the docs
        # say "of any size". The pace-by-playout path is the robust
        # default; mark back-pressure remains as an extra guard.
        initial_fill_complete = False
        for i in range(0, len(bytes_), self._PREWARM_CHUNK_BYTES):
            if not self._is_speaking:
                break  # barge-in mid-buffer — stop now
            await self._wait_for_mark_window()
            if not self._is_speaking:
                break
            chunk = bytes_[i : i + self._PREWARM_CHUNK_BYTES]
            if not first_chunk_sent:
                first_chunk_sent = True
            if self._aec is not None:
                self._aec.push_far_end(chunk)
            await self.audio_sender.send_audio(chunk)
            self._mark_first_audio_sent()
            mark_awaitable = await self._send_mark_awaitable()
            if (
                not initial_fill_complete
                and len(self._pending_marks) >= self._FIRST_MESSAGE_MARK_WINDOW
            ):
                initial_fill_complete = True
            # Telnyx has no mark concept — always pace by playout time.
            # Twilio: the first ``_FIRST_MESSAGE_MARK_WINDOW`` chunks go
            # out in burst to pre-fill the PSTN jitter buffer, then
            # playout-time pacing kicks in (via the sticky
            # ``initial_fill_complete`` flag) to prevent batch-ACK bursts
            # from draining the buffer → crackling.
            if mark_awaitable is None or initial_fill_complete:
                playout_ms = max(
                    1,
                    len(chunk) // self._PCM16_16K_BYTES_PER_MS,
                )
                await asyncio.sleep(playout_ms / 1000.0)
        return first_chunk_sent

    async def cleanup(self) -> None:
        """Cancel the STT loop and close STT/TTS/remote-message adapters."""
        # Abort any in-flight LLM stream and close any in-flight TTS WS so
        # the run_pipeline_llm / synthesize awaits unblock immediately
        # instead of waiting up to 30 s for their own watchdog timers.
        # Without this, the carrier's stop event ends the call but a
        # pending TTS WS frame-wait fires a stale "LLM loop error" /
        # "TTS streaming error" log line tens of seconds later. Parity
        # with TS ``StreamHandler.handleStop`` / ``handleWsClose``.
        cancel_event = getattr(self, "_llm_cancel_event", None)
        if cancel_event is not None:
            cancel_event.set()
        _tts_cancel = getattr(getattr(self, "_tts", None), "cancel_active_stream", None)
        if callable(_tts_cancel):
            try:
                _tts_cancel()
            except Exception:
                pass
        # Drop any pending barge-in timeout BEFORE we tear down metrics /
        # adapters. Without this, a call that ends while a barge-in is
        # pending leaves an asyncio.Task scheduled to fire
        # ``_barge_in_confirm_s`` later and call
        # ``metrics.record_overlap_end`` on a finalised metrics object —
        # a slow leak in long-running servers and a race producing
        # spurious overlap_end events. Idempotent: safe to call when no
        # pending state exists.
        self._clear_pending_barge_in()
        # Resolve every pending firstMessage mark future before tearing
        # down adapters. Without this, a call that ends abnormally mid
        # firstMessage (carrier WS drop, hangup during the paced sender)
        # leaves orphan ``asyncio.Future`` instances awaited by the send
        # loop that nothing will ever resolve.
        if getattr(self, "_pending_marks", None) is not None:
            self._drain_pending_marks()
        # Reset the firstMessage mark counter so a re-used handler
        # instance starts ``fm_<n>`` numbering at 1 on the next call.
        # See ``_send_paced_first_message_bytes`` for the per-send reset
        # that protects the within-call path.
        self._first_message_mark_counter = 0
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
