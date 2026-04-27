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

from getpatter.handlers.common import (
    _create_stt_from_config,
    _create_tts_from_config,
    _resolve_variables,
    _sanitize_variable_value,
    _validate_e164,
)
from getpatter.models import HookContext
from getpatter.observability.tracing import SPAN_STT, SPAN_TTS, start_span
from getpatter.services.pipeline_hooks import PipelineHookExecutor
from getpatter.services.sentence_chunker import SentenceChunker
from getpatter.utils.log_sanitize import mask_phone_number, sanitize_log_value

logger = logging.getLogger("getpatter")


# ---------------------------------------------------------------------------
# Shared tool definitions injected into every agent
# ---------------------------------------------------------------------------

# Short words / phrases that Whisper (and, less often, Deepgram) routinely
# emit when fed silence or TTS echo on mulaw 8 kHz. Dropping them as turns
# prevents the caller from entering a feedback loop where every silent frame
# triggers a new LLM+TTS turn. Parity with TS ``HALLUCINATIONS``.
_STT_HALLUCINATIONS: frozenset[str] = frozenset({
    "you", "thank you", "thanks", "yeah", "yes", "no",
    "okay", "ok", "uh", "um", "mmm", "hmm", ".", "bye",
    "right", "cool",
})


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
    from getpatter.models import Agent as _Agent, STTConfig as _STTCfg, TTSConfig as _TTSCfg
    from dataclasses import asdict

    fields: dict = {}
    for k in ("system_prompt", "voice", "model", "language", "first_message", "provider"):
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
    if provider == "pipeline":
        # Prefer the explicit ``provider_key`` ClassVar declared by
        # wrapper classes (stable, matches ``pricing.py`` keys); fall
        # back to the legacy ``provider`` instance attribute.
        if agent.stt is not None:
            stt_name = (
                getattr(type(agent.stt), "provider_key", None)
                or getattr(agent.stt, "provider", "")
            )
        else:
            stt_name = "deepgram" if deepgram_key else ""
        if agent.tts is not None:
            tts_name = (
                getattr(type(agent.tts), "provider_key", None)
                or getattr(agent.tts, "provider", "")
            )
        else:
            tts_name = "elevenlabs" if elevenlabs_key else ""
    elif provider == "openai_realtime":
        stt_name = "openai"
        tts_name = "openai"
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
    )


def evaluate_guardrails(agent, response_text: str) -> tuple[bool, str]:
    """Evaluate output guardrails against response text.

    Returns (blocked, guard_name). If blocked is True, the response should
    be suppressed.
    """
    guardrails = getattr(agent, "guardrails", None) or []
    for guard in guardrails:
        blocked = False
        blocked_terms = guard.get("blocked_terms") if isinstance(guard, dict) else getattr(guard, "blocked_terms", None)
        check_fn = guard.get("check") if isinstance(guard, dict) else getattr(guard, "check", None)
        guard_name = guard.get("name") if isinstance(guard, dict) else getattr(guard, "name", "unnamed")
        if blocked_terms:
            blocked = any(term.lower() in response_text.lower() for term in blocked_terms)
        if check_fn and not blocked:
            try:
                blocked = bool(check_fn(response_text))
            except Exception as exc:
                logger.warning("Guardrail '%s' check error: %s", guard_name, exc)
        if blocked:
            logger.warning("Guardrail '%s' triggered on: %.50s", guard_name, response_text)
            return True, guard_name
    return False, ""


def get_guardrail_replacement(agent, guard_name: str) -> str:
    """Get the replacement text for a triggered guardrail by name.

    Returns the replacement text from the specific guard that fired,
    falling back to a default message.
    """
    guardrails = getattr(agent, "guardrails", None) or []
    for guard in guardrails:
        name = guard.get("name") if isinstance(guard, dict) else getattr(guard, "name", "unnamed")
        if name == guard_name:
            r = (guard.get("replacement") if isinstance(guard, dict) else getattr(guard, "replacement", None))
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
        self._background_task: asyncio.Task | None = None

        # Create one EventBus per handler instance and wire it to metrics.
        from getpatter.observability.event_bus import EventBus as _EventBus
        self._event_bus: _EventBus = _EventBus()
        if self.metrics is not None and hasattr(self.metrics, "attach_event_bus"):
            self.metrics.attach_event_bus(self._event_bus)

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

    async def start(self) -> None:
        from getpatter.providers.openai_realtime import OpenAIRealtimeAdapter  # type: ignore[import]

        agent_tools: list[dict] = [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("parameters", {}),
            }
            for t in (self.agent.tools or [])
        ]
        openai_tools: list[dict] = agent_tools + [TRANSFER_CALL_TOOL, END_CALL_TOOL]

        self._adapter = OpenAIRealtimeAdapter(
            api_key=self._openai_key,
            model=self.agent.model,
            voice=self.agent.voice,
            instructions=self.resolved_prompt,
            language=self.agent.language,
            tools=openai_tools,
            audio_format=self._audio_format,
        )
        await self._adapter.connect()
        logger.debug("OpenAI Realtime connected")

        if self.agent.first_message:
            # Start measuring latency for the firstMessage turn (sendText →
            # first audio byte). Parity with TS handler.
            if self.metrics is not None:
                self.metrics.start_turn()
            await self._adapter.send_text(self.agent.first_message)

        self._background_task = asyncio.create_task(self._forward_events())

    async def _forward_events(self) -> None:
        from getpatter.services.tool_executor import ToolExecutor  # type: ignore[import]

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
                    if waiting_first_audio and self.metrics is not None:
                        self.metrics.record_tts_first_byte()
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

                    self.conversation_history.append(
                        {"role": "user", "text": ev_data, "timestamp": time.time()}
                    )
                    self.transcript_entries.append(
                        {"role": "user", "text": ev_data}
                    )
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
                        blocked, guard_name = evaluate_guardrails(self.agent, response_text)
                        if blocked:
                            await self._adapter.cancel_response()
                            replacement = get_guardrail_replacement(self.agent, guard_name)
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
                    waiting_first_audio = False
                    current_agent_text = ""

                elif ev_type == "response_done":
                    if self.metrics is not None and isinstance(ev_data, dict):
                        usage = ev_data.get("usage", {})
                        if usage:
                            self.metrics.record_realtime_usage(usage)
                    if current_agent_text:
                        # Push complete response as single history entry
                        self.conversation_history.append(
                            {"role": "assistant", "text": current_agent_text, "timestamp": time.time()}
                        )
                        self.transcript_entries.append(
                            {"role": "assistant", "text": current_agent_text}
                        )
                        if self.metrics is not None:
                            turn = self.metrics.record_turn_complete(current_agent_text)
                            await self._emit_turn_metrics(turn)
                        current_agent_text = ""
                    elif self.metrics is not None and self.metrics.turn_active:
                        # response_done without agent text = cancelled / empty
                        # response. Close the active turn as interrupted so the
                        # next speech_stopped can start a fresh turn cleanly.
                        # Parity with TS handleAdapterEvent response_done path.
                        self.metrics.record_turn_interrupted()
                    waiting_first_audio = True

                elif ev_type == "function_call":
                    func_data = ev_data
                    if func_data["name"] == "transfer_call":
                        raw_args = func_data.get("arguments", "{}")
                        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                        transfer_number = args.get("number", "")
                        if not _validate_e164(transfer_number):
                            logger.warning(
                                "transfer_call rejected: invalid number %s",
                                mask_phone_number(transfer_number),
                            )
                            await self._adapter.send_function_result(
                                func_data["call_id"],
                                json.dumps({"error": "Invalid phone number format", "status": "rejected"}),
                            )
                            continue
                        logger.debug(
                            "Transferring call to %s", mask_phone_number(transfer_number)
                        )
                        await self._adapter.send_function_result(
                            func_data["call_id"],
                            json.dumps({"status": "transferring", "to": transfer_number}),
                        )
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
                        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                        reason = args.get("reason", "conversation_complete")
                        logger.debug("Ending call: %s", reason)
                        await self._adapter.send_function_result(
                            func_data["call_id"],
                            json.dumps({"status": "ending", "reason": reason}),
                        )
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
                            (t for t in (self.agent.tools or []) if t["name"] == func_data["name"]),
                            None,
                        )
                        if tool_def and (tool_def.get("webhook_url") or tool_def.get("handler")):
                            args = func_data.get("arguments", "{}")
                            if isinstance(args, str):
                                args = json.loads(args)
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
                            )
                            await self._adapter.send_function_result(
                                func_data["call_id"], result
                            )
        except Exception as exc:
            logger.exception("OpenAI Realtime forward error: %s", exc)

    async def on_audio_received(self, audio_bytes: bytes) -> None:
        if self._adapter is None:
            return
        if self._input_transcode == "pcm16_16k_to_g711_ulaw":
            from getpatter.services.transcoding import pcm16_to_mulaw
            # Use per-handler StatefulResampler to preserve ratecv filter state
            # across chunks and prevent boundary artefacts.
            if self._resampler_16k_to_8k is None:
                from getpatter.services.transcoding import create_resampler_16k_to_8k
                self._resampler_16k_to_8k = create_resampler_16k_to_8k()
            audio_bytes = pcm16_to_mulaw(self._resampler_16k_to_8k.process(audio_bytes))
        await self._adapter.send_audio(audio_bytes)

    async def on_dtmf(self, digit: str) -> None:
        if self._adapter is not None:
            await self._adapter.send_text(
                f"The user pressed key {digit} on their phone keypad."
            )

    async def cleanup(self) -> None:
        if self._background_task:
            self._background_task.cancel()
            try:
                await self._background_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._adapter:
            await self._adapter.close()
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
        self._adapter = None
        # Per-handler StatefulResampler for Twilio mulaw 8 kHz -> PCM16 16 kHz.
        self._resampler_8k_to_16k = None

    async def start(self) -> None:
        from getpatter.providers.elevenlabs_convai import ElevenLabsConvAIAdapter  # type: ignore[import]

        voice = self.agent.voice if self.agent.voice != "alloy" else "EXAVITQu4vr4xnSDxMaL"
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

        self._adapter = ElevenLabsConvAIAdapter(
            api_key=self._elevenlabs_key,
            agent_id=agent_id,
            voice_id=voice,
            language=self.agent.language,
            first_message=self.agent.first_message,
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
                    self.transcript_entries.append(
                        {"role": "user", "text": ev_data}
                    )
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
                        blocked, _ = evaluate_guardrails(self.agent, response_text)
                        if blocked:
                            current_agent_text = ""
                        else:
                            current_agent_text += response_text

                elif ev_type == "response_done":
                    if current_agent_text:
                        self.conversation_history.append(
                            {"role": "assistant", "text": current_agent_text, "timestamp": time.time()}
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
        if self._adapter is not None:
            # ElevenLabs ConvAI expects PCM 16kHz. Twilio sends mulaw 8kHz.
            if self._for_twilio:
                from getpatter.services.transcoding import mulaw_to_pcm16
                # Use per-handler StatefulResampler to preserve ratecv state.
                if self._resampler_8k_to_16k is None:
                    from getpatter.services.transcoding import create_resampler_8k_to_16k
                    self._resampler_8k_to_16k = create_resampler_8k_to_16k()
                pcm16k = self._resampler_8k_to_16k.process(mulaw_to_pcm16(audio_bytes))
                await self._adapter.send_audio(pcm16k)
            else:
                await self._adapter.send_audio(audio_bytes)

    async def cleanup(self) -> None:
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
        self._stt_task: asyncio.Task | None = None
        self._is_speaking = False
        # Monotonic counter incremented at every TTS-start. ``_end_speaking_with_grace``
        # captures the value at scheduling time and only flips ``_is_speaking`` to
        # False if no new turn started in the meantime. Prevents an in-flight grace
        # task from clobbering the speaking flag of the *next* turn (mirrors TS).
        self._speaking_generation: int = 0
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
            self._tts = ElevenLabsTTS(api_key=self._elevenlabs_key, voice_id=self.agent.voice)

        if self._stt is None:
            logger.warning("Pipeline mode: no STT configured")
        if self._tts is None:
            logger.warning("Pipeline mode: no TTS configured")

        if self._stt is not None:
            await self._stt.connect()

        logger.debug("Pipeline mode: STT + TTS connected")

        # Play first_message if configured and no on_message handler.
        # Measure TTS-first-byte latency for parity with TS (`stream-handler.ts`).
        if self.agent.first_message and self.on_message is None and self._tts is not None:
            if self.metrics is not None:
                self.metrics.start_turn()
            first_chunk_sent = False
            # Drop any stale PCM16 carry byte from a prior synth (none at call
            # start, but defensive for parity with TS ``ttsByteCarry = null``).
            self.audio_sender.reset_pcm_carry()
            try:
                async for audio_chunk in self._tts.synthesize(self.agent.first_message):
                    if not first_chunk_sent:
                        first_chunk_sent = True
                        if self.metrics is not None:
                            self.metrics.record_tts_first_byte()
                    await self.audio_sender.send_audio(audio_chunk)
            finally:
                # Drop any partial int16 byte to prevent cross-turn corruption
                # if the stream threw before a complete sample was delivered.
                self.audio_sender.reset_pcm_carry()
            if first_chunk_sent and self.metrics is not None:
                turn = self.metrics.record_turn_complete(self.agent.first_message)
                self.conversation_history.append(
                    {"role": "assistant", "text": self.agent.first_message, "timestamp": time.time()}
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
            from getpatter.services.tool_executor import ToolExecutor

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
            )

        # Create remote message handler once if on_message is a remote URL
        from getpatter.services.remote_message import is_remote_url, RemoteMessageHandler
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

                # afterSynthesize hook (per-chunk)
                processed_audio = await hook_executor.run_after_synthesize(
                    audio_chunk, processed, hook_ctx
                )
                if processed_audio is None:
                    continue  # hook discarded this chunk

                if first_tts_chunk[0] and self.metrics is not None:
                    self.metrics.record_tts_first_byte()
                    first_tts_chunk[0] = False
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
        chunker = SentenceChunker()
        full_response_parts: list[str] = []
        self._begin_speaking()
        first_tts_chunk = [True]
        llm_first_token_sent = [True]  # Fix 5: track LLM TTFT

        hooks = getattr(self.agent, "hooks", None)
        hook_executor = PipelineHookExecutor(hooks)
        hook_ctx = self._build_hook_context()

        interrupted = False
        llm_error = False
        try:
            try:
                async for token in result:
                    full_response_parts.append(token)
                    # Fix 5: record LLM first-token (TTFT).
                    if llm_first_token_sent[0] and self.metrics is not None:
                        self.metrics.record_llm_first_token()
                        llm_first_token_sent[0] = False

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

                        if not await self._synthesize_sentence(sentence, hook_executor, hook_ctx, first_tts_chunk):
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

                    if not await self._synthesize_sentence(sentence, hook_executor, hook_ctx, first_tts_chunk):
                        interrupted = True
                        break
        finally:
            # Schedule the flip to idle. Keeps the speaking flag set during
            # the audio tail still playing on the carrier so STT echo on
            # the trailing samples doesn't look like a fresh user turn.
            await self._end_speaking_with_grace()

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

        self.conversation_history.append(
            {"role": "assistant", "text": response_text, "timestamp": time.time()}
        )
        self.transcript_entries.append(
            {"role": "assistant", "text": response_text}
        )
        # Use sentence chunking + hooks for consistent behavior with streaming path
        hooks = getattr(self.agent, "hooks", None)
        hook_executor = PipelineHookExecutor(hooks)
        hook_ctx = self._build_hook_context()

        chunker = SentenceChunker()
        sentences = chunker.push(response_text) + chunker.flush()
        if not sentences:
            sentences = [response_text] if response_text else []

        self._begin_speaking()
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
        if self.metrics is not None:
            self.metrics.record_overlap_start()
        logger.debug(
            "Barge-in: caller spoke over agent (%s)",
            sanitize_log_value(transcript.text[:40]),
        )
        self._is_speaking = False
        try:
            await self.audio_sender.send_clear()
        except Exception as exc:
            logger.debug("send_clear during barge-in failed: %s", exc)
        if self.metrics is not None:
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
                since_last, normalised[:40],
            )
            return False
        if since_last < 0.5:
            logger.debug(
                "Dropped back-to-back final transcript (%.2fs since last): %r",
                since_last, normalised[:40],
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
                if not (transcript.is_final and transcript.text):
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
                    continue

                if self.metrics is not None:
                    self.metrics.record_on_user_turn_completed_delay(0.0)
                if self.on_message is None and self._llm_loop is None:
                    # No message handler or LLM loop — discard orphaned turn
                    if self.metrics is not None:
                        self.metrics.record_turn_interrupted()
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
                    result = self._llm_loop.run(
                        filtered_text,
                        list(self.conversation_history),
                        call_ctx,
                    )
                    response_text = await self._process_streaming_response(result, self.call_id)
                    if response_text:
                        self.conversation_history.append(
                            {"role": "assistant", "text": response_text, "timestamp": time.time()}
                        )
                        self.transcript_entries.append(
                            {"role": "assistant", "text": response_text}
                        )
                    continue

                # on_message handler path
                if self.metrics is not None:
                    self.metrics.record_turn_committed()
                msg_data = {
                    "text": filtered_text,
                    "call_id": self.call_id,
                    "caller": self.caller,
                    "callee": self.callee,
                    "history": list(self.conversation_history),
                }

                response_text = ""
                streaming = False

                from getpatter.services.remote_message import is_remote_url, is_websocket_url
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
                        self.conversation_history.append(
                            {"role": "assistant", "text": response_text, "timestamp": time.time()}
                        )
                        self.transcript_entries.append(
                            {"role": "assistant", "text": response_text}
                        )
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
            from getpatter.services.transcoding import mulaw_to_pcm16
            # Use per-handler StatefulResampler to preserve ratecv filter state
            # across audio chunks (prevents boundary artefacts at STT input).
            if self._resampler_8k_to_16k is None:
                from getpatter.services.transcoding import create_resampler_8k_to_16k
                self._resampler_8k_to_16k = create_resampler_8k_to_16k()
            pcm = self._resampler_8k_to_16k.process(mulaw_to_pcm16(audio_bytes))
        else:
            pcm = audio_bytes

        # ---- VAD wiring (Fix 8) ----
        # Optional ``agent.vad`` runs *before* STT so we can react to
        # speech_start with immediate barge-in (clearing the carrier audio
        # buffer) rather than waiting for the STT engine's slower endpoint.
        vad = getattr(self.agent, "vad", None)
        if vad is not None:
            try:
                vad_event = await vad.process_frame(pcm, 16000)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("VAD process_frame failed: %s", exc)
                vad_event = None
            if vad_event is not None:
                if vad_event.type == "speech_start":
                    if self._is_speaking:
                        # Caller spoke over in-flight TTS — preempt now.
                        try:
                            await self.audio_sender.send_clear()
                        except Exception as exc:
                            logger.debug("send_clear during VAD barge-in failed: %s", exc)
                        if self.metrics is not None:
                            self.metrics.record_turn_interrupted()
                        # Force-flip immediately and bump the generation so a
                        # pending grace-flip from the prior turn can't fight us.
                        self._is_speaking = False
                        self._speaking_generation += 1
                    if self.metrics is not None:
                        self.metrics.start_turn_if_idle()
                elif vad_event.type == "speech_end":
                    if self.metrics is not None:
                        self.metrics.record_vad_stop()

            # Self-hearing guard: while the agent is speaking, don't pass
            # caller audio to STT — VAD already gave us authoritative
            # barge-in detection above, so any STT audio sent here would
            # just be the agent's own TTS echoing back.
            if self._is_speaking:
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

    def _begin_speaking(self) -> None:
        """Mark TTS playback as in-progress and bump the generation counter.

        The generation counter is consulted by ``_end_speaking_with_grace``
        so a delayed flip-to-idle from a previous turn cannot cancel the
        speaking flag of the *current* turn.
        """
        self._speaking_generation += 1
        self._is_speaking = True

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
        if grace_ms <= 0:
            self._is_speaking = False
            return

        gen = self._speaking_generation

        async def _flip_after_grace() -> None:
            try:
                await asyncio.sleep(grace_ms / 1000)
                # Only reset if no newer turn started while we slept; a
                # newer turn would have bumped ``_speaking_generation``.
                if self._speaking_generation == gen:
                    self._is_speaking = False
            except asyncio.CancelledError:  # pragma: no cover
                raise
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("tts grace flip failed: %s", exc)

        asyncio.create_task(_flip_after_grace())

    async def cleanup(self) -> None:
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
                            usd = req_resp.json().get("response", {}).get("details", {}).get("usd", None)
                            if usd is not None:
                                metrics.set_actual_stt_cost(float(usd))
                                logger.debug("Deepgram actual cost: $%s", usd)
    except Exception as exc:
        logger.debug("Could not fetch Deepgram request cost: %s", exc)
