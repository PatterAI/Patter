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
import base64
import inspect
import json
import logging
import re
import time
from abc import ABC, abstractmethod
from collections import deque
from typing import Any, Callable

from patter.handlers.common import (
    _create_stt_from_config,
    _create_tts_from_config,
    _resolve_variables,
    _sanitize_variable_value,
    _validate_e164,
)
from patter.models import HookContext
from patter.services.pipeline_hooks import PipelineHookExecutor
from patter.services.sentence_chunker import SentenceChunker
from patter.utils.log_sanitize import mask_phone_number, sanitize_log_value

logger = logging.getLogger("patter")


# ---------------------------------------------------------------------------
# Shared tool definitions injected into every agent
# ---------------------------------------------------------------------------

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
    from patter.models import Agent as _Agent, STTConfig as _STTCfg, TTSConfig as _TTSCfg
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
        logger.info("Per-call config overrides applied: %s", list(fields.keys()))
    return agent


def create_metrics_accumulator(
    call_id: str,
    provider: str,
    telephony_provider: str,
    agent,
    deepgram_key: str,
    elevenlabs_key: str,
    pricing: dict | None,
):
    """Create and return a CallMetricsAccumulator for the call."""
    from patter.services.metrics import CallMetricsAccumulator

    stt_name = ""
    tts_name = ""
    if provider == "pipeline":
        stt_name = getattr(agent.stt, "provider", "") if agent.stt else ("deepgram" if deepgram_key else "")
        tts_name = getattr(agent.tts, "provider", "") if agent.tts else ("elevenlabs" if elevenlabs_key else "")
    elif provider == "openai_realtime":
        stt_name = "openai"
        tts_name = "openai"
    elif provider == "elevenlabs_convai":
        stt_name = "elevenlabs"
        tts_name = "elevenlabs"
    llm_name = "openai" if provider == "openai_realtime" else ("elevenlabs" if provider == "elevenlabs_convai" else "custom")
    return CallMetricsAccumulator(
        call_id=call_id,
        provider_mode=provider,
        telephony_provider=telephony_provider,
        stt_provider=stt_name,
        tts_provider=tts_name,
        llm_provider=llm_name,
        pricing=pricing,
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
        self._adapter = None

    async def start(self) -> None:
        from patter.providers.openai_realtime import OpenAIRealtimeAdapter  # type: ignore[import]
        from patter.services.tool_executor import ToolExecutor  # type: ignore[import]

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
        logger.info("OpenAI Realtime connected")

        if self.agent.first_message:
            await self._adapter.send_text(self.agent.first_message)

        self._background_task = asyncio.create_task(self._forward_events())

    async def _forward_events(self) -> None:
        from patter.services.tool_executor import ToolExecutor  # type: ignore[import]

        tool_executor = ToolExecutor()
        waiting_first_audio = False
        current_agent_text = ""
        try:
            async for ev_type, ev_data in self._adapter.receive_events():
                if ev_type == "audio":
                    if waiting_first_audio and self.metrics is not None:
                        self.metrics.record_tts_first_byte()
                        waiting_first_audio = False
                    await self.audio_sender.send_audio(ev_data)
                    await self.audio_sender.send_mark(f"audio_{id(ev_data)}")

                elif ev_type == "transcript_input":
                    logger.info("User: %s", sanitize_log_value(ev_data))
                    if self.metrics is not None:
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
                            if self.on_metrics:
                                await self.on_metrics(
                                    {
                                        "call_id": self.call_id,
                                        "turn": turn,
                                        "cost_so_far": self.metrics.get_cost_so_far(),
                                    }
                                )
                        current_agent_text = ""

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
                        logger.info(
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
                        logger.info("Ending call: %s", reason)
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
        if self._adapter is not None:
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

    async def start(self) -> None:
        from patter.providers.elevenlabs_convai import ElevenLabsConvAIAdapter  # type: ignore[import]

        voice = self.agent.voice if self.agent.voice != "alloy" else "21m00Tcm4TlvDq8ikWAM"
        agent_id = ""
        el_config = getattr(self.agent, "elevenlabs_convai", None) or {}
        if isinstance(el_config, dict):
            agent_id = el_config.get("agent_id", "")

        self._adapter = ElevenLabsConvAIAdapter(
            api_key=self._elevenlabs_key,
            agent_id=agent_id,
            voice_id=voice,
            language=self.agent.language,
            first_message=self.agent.first_message,
        )
        await self._adapter.connect()
        logger.info("ElevenLabs ConvAI connected")

        self._background_task = asyncio.create_task(self._forward_events())

    async def _forward_events(self) -> None:
        waiting_first_audio = False
        current_agent_text = ""
        try:
            async for ev_type, ev_data in self._adapter.receive_events():
                if ev_type == "audio":
                    if waiting_first_audio and self.metrics is not None:
                        self.metrics.record_tts_first_byte()
                        waiting_first_audio = False
                    await self.audio_sender.send_audio(ev_data)

                elif ev_type == "transcript_input":
                    logger.info("User: %s", sanitize_log_value(ev_data))
                    if self.metrics is not None:
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
                            if self.on_metrics:
                                await self.on_metrics(
                                    {
                                        "call_id": self.call_id,
                                        "turn": turn,
                                        "cost_so_far": self.metrics.get_cost_so_far(),
                                    }
                                )
                        current_agent_text = ""

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
                from patter.services.transcoding import mulaw_to_pcm16, resample_8k_to_16k
                pcm16k = resample_8k_to_16k(mulaw_to_pcm16(audio_bytes))
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
        transfer_fn=None,
        hangup_fn=None,
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
        self._transfer_fn = transfer_fn
        self._hangup_fn = hangup_fn
        self._stt = None
        self._tts = None
        self._stt_task: asyncio.Task | None = None
        self._is_speaking = False
        self._call_control = None
        self._llm_loop = None
        self._msg_accepts_call = False
        self._remote_handler = None

    async def start(self) -> None:
        from patter.models import CallControl

        # Create STT
        if self.agent.stt:
            self._stt = _create_stt_from_config(self.agent.stt, for_twilio=self._for_twilio)
        elif self._deepgram_key:
            from patter.providers.deepgram_stt import DeepgramSTT  # type: ignore[import]
            if self._for_twilio:
                self._stt = DeepgramSTT.for_twilio(api_key=self._deepgram_key, language=self.agent.language)
            else:
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
            from patter.providers.elevenlabs_tts import ElevenLabsTTS  # type: ignore[import]
            voice_id = self.agent.voice if self.agent.voice != "alloy" else "21m00Tcm4TlvDq8ikWAM"
            self._tts = ElevenLabsTTS(api_key=self._elevenlabs_key, voice_id=voice_id)

        if self._stt is None:
            logger.warning("Pipeline mode: no STT configured")
        if self._tts is None:
            logger.warning("Pipeline mode: no TTS configured")

        if self._stt is not None:
            await self._stt.connect()

        logger.info("Pipeline mode: STT + TTS connected")

        # Play first_message if configured and no on_message handler
        if self.agent.first_message and self.on_message is None and self._tts is not None:
            async for audio_chunk in self._tts.synthesize(self.agent.first_message):
                await self.audio_sender.send_audio(audio_chunk)

        # CallControl for pipeline mode
        self._call_control = CallControl(
            call_id=self.call_id,
            caller=self.caller,
            callee=self.callee,
            telephony_provider="twilio" if self._for_twilio else "telnyx",
            _transfer_fn=self._transfer_fn,
            _hangup_fn=self._hangup_fn,
        )

        # Check if on_message accepts CallControl
        if self.on_message is not None and callable(self.on_message):
            try:
                sig = inspect.signature(self.on_message)
                self._msg_accepts_call = len(sig.parameters) >= 2
            except (ValueError, TypeError):
                pass

        # Built-in LLM loop
        if self.on_message is None and self._openai_key:
            from patter.services.llm_loop import LLMLoop
            from patter.services.tool_executor import ToolExecutor

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
            )

        # Create remote message handler once if on_message is a remote URL
        from patter.services.remote_message import is_remote_url, RemoteMessageHandler
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

        gen = self._tts.synthesize(processed)
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
        return True

    async def _process_streaming_response(self, result, call_id: str) -> str:
        """Process a streaming (async generator) response through TTS with sentence chunking."""
        chunker = SentenceChunker()
        full_response_parts: list[str] = []
        self._is_speaking = True
        first_tts_chunk = [True]

        hooks = getattr(self.agent, "hooks", None)
        hook_executor = PipelineHookExecutor(hooks)
        hook_ctx = self._build_hook_context()

        interrupted = False
        llm_error = False
        try:
            try:
                async for token in result:
                    full_response_parts.append(token)

                    sentences = chunker.push(token)
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
            self._is_speaking = False  # guaranteed reset

        response_text = "".join(full_response_parts)

        if not interrupted and not llm_error and response_text:
            if self.metrics is not None:
                self.metrics.record_tts_complete(response_text)
                turn = self.metrics.record_turn_complete(response_text)
                if self.on_metrics:
                    await self.on_metrics(
                        {
                            "call_id": call_id,
                            "turn": turn,
                            "cost_so_far": self.metrics.get_cost_so_far(),
                        }
                    )
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

        self._is_speaking = True
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
            self._is_speaking = False  # guaranteed reset

        if not interrupted:
            if self.metrics is not None:
                self.metrics.record_tts_complete(response_text)
                turn = self.metrics.record_turn_complete(response_text)
                if self.on_metrics:
                    await self.on_metrics(
                        {
                            "call_id": call_id,
                            "turn": turn,
                            "cost_so_far": self.metrics.get_cost_so_far(),
                        }
                    )

    async def _stt_loop(self) -> None:
        try:
            async for transcript in self._stt.receive_transcripts():
                if not (transcript.is_final and transcript.text):
                    continue

                logger.info("User: %s", sanitize_log_value(transcript.text))

                if self.metrics is not None:
                    self.metrics.start_turn()
                    self.metrics.record_stt_complete(transcript.text)

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
                    logger.info("afterTranscribe hook vetoed turn")
                    if self.metrics is not None:
                        self.metrics.record_turn_interrupted()
                    continue

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
                msg_data = {
                    "text": filtered_text,
                    "call_id": self.call_id,
                    "caller": self.caller,
                    "callee": self.callee,
                    "history": list(self.conversation_history),
                }

                response_text = ""
                streaming = False

                from patter.services.remote_message import is_remote_url, is_websocket_url
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
                    await self._process_regular_response(response_text, self.call_id)

        except Exception as exc:
            logger.exception("Pipeline STT loop error: %s", exc)

    async def on_audio_received(self, audio_bytes: bytes) -> None:
        if self._stt is not None and not self._is_speaking:
            # Twilio sends mulaw 8kHz — convert to PCM 16kHz for STT providers.
            # Telnyx sends PCM 16kHz natively.
            if self._for_twilio:
                from patter.services.transcoding import mulaw_to_pcm16, resample_8k_to_16k
                pcm16k = resample_8k_to_16k(mulaw_to_pcm16(audio_bytes))
                await self._stt.send_audio(pcm16k)
            else:
                await self._stt.send_audio(audio_bytes)
            if self.metrics is not None:
                self.metrics.add_stt_audio_bytes(len(audio_bytes))

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
                                logger.info("Deepgram actual cost: $%s", usd)
    except Exception as exc:
        logger.debug("Could not fetch Deepgram request cost: %s", exc)
