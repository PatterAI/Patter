"""Twilio webhook and stream handlers for local mode."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from urllib.parse import quote

logger = logging.getLogger("patter")

# Maximum size (bytes) of a single WebSocket message accepted from Twilio.
# Twilio audio frames are ~160 bytes (mulaw 8 kHz, 20 ms).  1 MB is
# extremely generous and defends against memory exhaustion from a malformed
# or malicious stream peer.
_MAX_WS_MESSAGE_BYTES = 1 * 1024 * 1024


def _validate_e164(number: str) -> bool:
    """Return True if *number* is a valid E.164 phone number."""
    return bool(re.match(r'^\+[1-9]\d{6,14}$', number))


def _validate_twilio_sid(sid: str, prefix: str = "CA") -> bool:
    """Return True if *sid* looks like a valid Twilio SID.

    Twilio SIDs are exactly 34 characters: a 2-letter prefix followed by
    32 hex characters (e.g. CAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx).
    Validating before interpolating into REST API URLs prevents path
    traversal / SSRF against the Twilio API.
    """
    if len(sid) != 34:
        return False
    if not sid.startswith(prefix):
        return False
    return bool(re.match(r'^[A-Z]{2}[0-9a-f]{32}$', sid))


def _sanitize_variable_value(value: str) -> str:
    """Strip control characters and limit length to prevent prompt injection."""
    return re.sub(r'[\x00-\x1f\x7f]', '', str(value))[:500]


def _xml_escape(s: str) -> str:
    """Escape special XML characters."""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _resolve_variables(template: str, variables: dict) -> str:
    """Replace ``{key}`` placeholders in *template* with values from *variables*.

    Args:
        template: A string that may contain ``{key}`` placeholders.
        variables: Mapping of placeholder names to replacement values.

    Returns:
        A new string with all matching placeholders substituted.
    """
    result = template
    for key, value in variables.items():
        result = result.replace(f"{{{key}}}", str(value))
    return result


def _create_stt_from_config(config, for_twilio: bool = False):
    """Create an STT adapter from an STTConfig object.

    Args:
        config: An ``STTConfig`` instance (or ``None``).
        for_twilio: When ``True``, configure for Twilio's mulaw 8 kHz stream.
    """
    if config is None:
        return None
    provider = config.provider
    if provider == "deepgram":
        from patter.providers.deepgram_stt import DeepgramSTT  # type: ignore[import]

        if for_twilio:
            return DeepgramSTT.for_twilio(api_key=config.api_key, language=config.language)
        return DeepgramSTT(api_key=config.api_key, language=config.language)
    elif provider == "whisper":
        from patter.providers.whisper_stt import WhisperSTT  # type: ignore[import]

        return WhisperSTT(api_key=config.api_key, language=config.language)
    return None


def _create_tts_from_config(config):
    """Create a TTS adapter from a TTSConfig object.

    Args:
        config: A ``TTSConfig`` instance (or ``None``).
    """
    if config is None:
        return None
    provider = config.provider
    if provider == "elevenlabs":
        from patter.providers.elevenlabs_tts import ElevenLabsTTS  # type: ignore[import]

        return ElevenLabsTTS(api_key=config.api_key, voice_id=config.voice)
    elif provider == "openai":
        from patter.providers.openai_tts import OpenAITTS  # type: ignore[import]

        return OpenAITTS(api_key=config.api_key, voice=config.voice)
    return None


def twilio_webhook_handler(
    call_sid: str,
    caller: str,
    callee: str,
    webhook_base_url: str,
) -> str:
    """Generate TwiML response for an incoming Twilio call.

    Returns an XML string that tells Twilio to stream audio to our WebSocket.

    Args:
        call_sid: Twilio CallSid from the webhook.
        caller: The calling number (From).
        callee: The called number (To).
        webhook_base_url: Hostname (no scheme) of this server, e.g. "abc.ngrok.io".
    """
    # Lazy import — provider adapter may be created by the parallel agent
    from patter.providers.twilio_adapter import TwilioAdapter  # type: ignore[import]

    stream_url = (
        f"wss://{webhook_base_url}/ws/stream/{call_sid}"
        f"?caller={quote(caller)}&callee={quote(callee)}"
    )
    return TwilioAdapter.generate_stream_twiml(stream_url)


_TRANSFER_CALL_TOOL: dict = {
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

_END_CALL_TOOL: dict = {
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


async def twilio_stream_bridge(
    websocket,
    agent,
    openai_key: str,
    on_call_start=None,
    on_call_end=None,
    on_transcript=None,
    on_message=None,
    deepgram_key: str = "",
    elevenlabs_key: str = "",
    twilio_sid: str = "",
    twilio_token: str = "",
    recording: bool = False,
) -> None:
    """Bridge a Twilio WebSocket media stream to the configured AI provider.

    Supports two provider modes depending on ``agent.provider``:

    * ``"openai_realtime"`` (default) — streams mulaw audio directly to
      OpenAI Realtime API, which handles STT, LLM, and TTS.
    * ``"pipeline"`` — uses Deepgram for STT, calls ``on_message`` with the
      transcript, then synthesises the response with ElevenLabs TTS and sends
      it back to Twilio as mulaw audio.

    Args:
        websocket: A Starlette/FastAPI WebSocket instance.
        agent: An ``Agent`` dataclass with prompt, voice, model, tools, etc.
        openai_key: OpenAI API key for the Realtime API (openai_realtime mode).
        on_call_start: Optional async callable(dict) — fired when the stream starts.
        on_call_end: Optional async callable(dict) — fired when the stream ends.
        on_transcript: Optional async callable(dict) — fired for each user utterance.
        on_message: Optional async callable(dict) -> str — called with the user's
            text in pipeline mode; return value is synthesised and played back.
        deepgram_key: Deepgram API key (pipeline mode).
        elevenlabs_key: ElevenLabs API key (pipeline mode).
        twilio_sid: Twilio Account SID (for call transfer and recording).
        twilio_token: Twilio Auth Token (for call transfer and recording).
        recording: When ``True``, start recording the call via Twilio Recordings API.
    """
    await websocket.accept()

    caller: str = websocket.query_params.get("caller", "")
    callee: str = websocket.query_params.get("callee", "")

    stream_sid: str | None = None
    call_sid_actual: str = ""
    transcript_entries: list[dict] = []
    conversation_history: list[dict] = []

    # --- OpenAI Realtime mode state ---
    openai_adapter = None
    receive_task: asyncio.Task | None = None

    # --- ElevenLabs ConvAI mode state ---
    elevenlabs_adapter = None

    # --- Pipeline mode state ---
    stt = None
    tts = None
    stt_task: asyncio.Task | None = None
    is_speaking = False

    # --- Mark-based barge-in state ---
    chunk_count: int = 0
    last_confirmed_mark: str = ""

    try:
        while True:
            raw = await websocket.receive_text()
            if len(raw) > _MAX_WS_MESSAGE_BYTES:
                logger.warning(
                    "Oversized WebSocket message dropped (%d bytes)", len(raw)
                )
                continue
            data = json.loads(raw)
            event = data.get("event", "")

            if event == "start":
                stream_sid = data.get("streamSid", "")
                start_data = data.get("start", {})
                call_sid_actual = start_data.get("callSid", "")
                custom_params: dict = start_data.get("customParameters", {})

                logger.info("Call started: %s", call_sid_actual)
                if custom_params:
                    logger.info("Custom params: %s", custom_params)

                # Fire on_call_start callback
                if on_call_start:
                    await on_call_start(
                        {
                            "call_id": call_sid_actual,
                            "caller": caller,
                            "callee": callee,
                            "direction": "inbound",
                            "custom_params": custom_params,
                        }
                    )

                # Start recording if requested
                if recording and twilio_sid and twilio_token and call_sid_actual:
                    if not _validate_twilio_sid(call_sid_actual, "CA"):
                        logger.warning(
                            "Recording skipped: invalid CallSid format %r",
                            call_sid_actual,
                        )
                    else:
                        import httpx as _httpx

                        try:
                            async with _httpx.AsyncClient() as _http:
                                await _http.post(
                                    f"https://api.twilio.com/2010-04-01/Accounts/{twilio_sid}/Calls/{call_sid_actual}/Recordings.json",
                                    auth=(twilio_sid, twilio_token),
                                )
                            logger.info("Recording started for %s", call_sid_actual)
                        except Exception as _exc:
                            logger.warning("Could not start recording: %s", _exc)

                # Resolve dynamic variables in system prompt.
                # agent.variables (if set) take precedence; custom_params from TwiML are merged in.
                resolved_prompt = agent.system_prompt
                agent_variables: dict = getattr(agent, "variables", None) or {}
                all_variables = {**agent_variables}
                for k, v in custom_params.items():
                    all_variables[k] = _sanitize_variable_value(v)
                if all_variables:
                    resolved_prompt = _resolve_variables(resolved_prompt, all_variables)

                provider = getattr(agent, "provider", "openai_realtime")

                if provider == "pipeline":
                    # ---- Pipeline mode: configurable STT + TTS ----
                    from patter.services.transcoding import (  # type: ignore[import]
                        pcm16_to_mulaw,
                        resample_16k_to_8k,
                    )

                    # Create STT: prefer agent.stt config, fall back to deepgram_key
                    if agent.stt:
                        stt = _create_stt_from_config(agent.stt, for_twilio=True)
                    elif deepgram_key:
                        from patter.providers.deepgram_stt import DeepgramSTT  # type: ignore[import]

                        stt = DeepgramSTT.for_twilio(api_key=deepgram_key, language=agent.language)
                    else:
                        stt = None

                    # Create TTS: prefer agent.tts config, fall back to elevenlabs_key
                    if agent.tts:
                        tts = _create_tts_from_config(agent.tts)
                    elif elevenlabs_key:
                        from patter.providers.elevenlabs_tts import ElevenLabsTTS  # type: ignore[import]

                        _voice_id = agent.voice if agent.voice != "alloy" else "21m00Tcm4TlvDq8ikWAM"
                        tts = ElevenLabsTTS(api_key=elevenlabs_key, voice_id=_voice_id)
                    else:
                        tts = None

                    if stt is None:
                        logger.warning("Pipeline mode: no STT configured")
                    if tts is None:
                        logger.warning("Pipeline mode: no TTS configured")

                    if stt is not None:
                        await stt.connect()

                    logger.info("Pipeline mode: STT + TTS connected")

                    # If agent has a first_message, synthesise and play it now
                    if agent.first_message and on_message is None and tts is not None:
                        async for audio_chunk in tts.synthesize(agent.first_message):
                            resampled = resample_16k_to_8k(audio_chunk)
                            mulaw = pcm16_to_mulaw(resampled)
                            encoded = base64.b64encode(mulaw).decode("ascii")
                            await websocket.send_text(
                                json.dumps(
                                    {
                                        "event": "media",
                                        "streamSid": stream_sid,
                                        "media": {"payload": encoded},
                                    }
                                )
                            )

                    # STT receive loop — runs concurrently with audio ingestion
                    async def stt_loop() -> None:
                        nonlocal is_speaking
                        try:
                            async for transcript in stt.receive_transcripts():
                                if not (transcript.is_final and transcript.text):
                                    continue

                                logger.info("User: %s", transcript.text)
                                if len(conversation_history) >= 200:
                                    conversation_history.pop(0)
                                conversation_history.append(
                                    {
                                        "role": "user",
                                        "text": transcript.text,
                                        "timestamp": time.time(),
                                    }
                                )
                                if len(transcript_entries) >= 200:
                                    transcript_entries.pop(0)
                                transcript_entries.append(
                                    {"role": "user", "text": transcript.text}
                                )

                                if on_transcript:
                                    await on_transcript(
                                        {
                                            "role": "user",
                                            "text": transcript.text,
                                            "call_id": call_sid_actual,
                                            "history": list(conversation_history),
                                        }
                                    )

                                if on_message is None:
                                    continue

                                response_text = await on_message(
                                    {
                                        "text": transcript.text,
                                        "call_id": call_sid_actual,
                                        "caller": caller,
                                        "history": list(conversation_history),
                                    }
                                )

                                if not response_text:
                                    continue

                                if len(conversation_history) >= 200:
                                    conversation_history.pop(0)
                                conversation_history.append(
                                    {
                                        "role": "assistant",
                                        "text": response_text,
                                        "timestamp": time.time(),
                                    }
                                )
                                if len(transcript_entries) >= 200:
                                    transcript_entries.pop(0)
                                transcript_entries.append(
                                    {"role": "assistant", "text": response_text}
                                )
                                is_speaking = True
                                async for audio_chunk in tts.synthesize(response_text):
                                    if not is_speaking:
                                        break
                                    resampled = resample_16k_to_8k(audio_chunk)
                                    mulaw = pcm16_to_mulaw(resampled)
                                    encoded = base64.b64encode(mulaw).decode("ascii")
                                    await websocket.send_text(
                                        json.dumps(
                                            {
                                                "event": "media",
                                                "streamSid": stream_sid,
                                                "media": {"payload": encoded},
                                            }
                                        )
                                    )
                                is_speaking = False
                        except Exception as exc:
                            logger.exception("Pipeline STT loop error: %s", exc)

                    stt_task = asyncio.create_task(stt_loop())

                elif provider == "elevenlabs_convai":
                    # ---- ElevenLabs Conversational AI mode ----
                    from patter.providers.elevenlabs_convai import ElevenLabsConvAIAdapter  # type: ignore[import]
                    from patter.services.transcoding import (  # type: ignore[import]
                        pcm16_to_mulaw,
                        resample_16k_to_8k,
                    )

                    _el_voice = agent.voice if agent.voice != "alloy" else "21m00Tcm4TlvDq8ikWAM"
                    _el_agent_id = ""
                    _el_config = getattr(agent, "elevenlabs_convai", None) or {}
                    if isinstance(_el_config, dict):
                        _el_agent_id = _el_config.get("agent_id", "")

                    elevenlabs_adapter = ElevenLabsConvAIAdapter(
                        api_key=elevenlabs_key,
                        agent_id=_el_agent_id,
                        voice_id=_el_voice,
                        language=agent.language,
                        first_message=agent.first_message,
                    )
                    await elevenlabs_adapter.connect()
                    logger.info("ElevenLabs ConvAI connected (Twilio)")

                    async def forward_elevenlabs_to_twilio() -> None:
                        try:
                            async for ev_type, ev_data in elevenlabs_adapter.receive_events():
                                if ev_type == "audio":
                                    # ElevenLabs returns PCM audio; transcode to mulaw for Twilio
                                    resampled = resample_16k_to_8k(ev_data)
                                    mulaw = pcm16_to_mulaw(resampled)
                                    encoded = base64.b64encode(mulaw).decode("ascii")
                                    await websocket.send_text(
                                        json.dumps(
                                            {
                                                "event": "media",
                                                "streamSid": stream_sid,
                                                "media": {"payload": encoded},
                                            }
                                        )
                                    )
                                elif ev_type == "transcript_input":
                                    logger.info("User: %s", ev_data)
                                    if len(conversation_history) >= 200:
                                        conversation_history.pop(0)
                                    conversation_history.append(
                                        {
                                            "role": "user",
                                            "text": ev_data,
                                            "timestamp": time.time(),
                                        }
                                    )
                                    if len(transcript_entries) >= 200:
                                        transcript_entries.pop(0)
                                    transcript_entries.append(
                                        {"role": "user", "text": ev_data}
                                    )
                                    if on_transcript:
                                        await on_transcript(
                                            {
                                                "role": "user",
                                                "text": ev_data,
                                                "call_id": call_sid_actual,
                                                "history": list(conversation_history),
                                            }
                                        )
                                elif ev_type == "transcript_output":
                                    if ev_data:
                                        response_text: str = ev_data
                                        # --- Apply output guardrails ---
                                        guardrails = getattr(agent, "guardrails", None) or []
                                        guardrail_triggered = False
                                        for guard in guardrails:
                                            blocked = False
                                            blocked_terms = guard.get("blocked_terms") if isinstance(guard, dict) else getattr(guard, "blocked_terms", None)
                                            check_fn = guard.get("check") if isinstance(guard, dict) else getattr(guard, "check", None)
                                            guard_name = guard.get("name") if isinstance(guard, dict) else getattr(guard, "name", "unnamed")
                                            if blocked_terms:
                                                blocked = any(
                                                    term.lower() in response_text.lower()
                                                    for term in blocked_terms
                                                )
                                            if check_fn and not blocked:
                                                try:
                                                    blocked = bool(check_fn(response_text))
                                                except Exception as _guard_exc:
                                                    logger.warning("Guardrail '%s' check error: %s", guard_name, _guard_exc)
                                            if blocked:
                                                logger.warning(
                                                    "Guardrail '%s' triggered on: %.50s",
                                                    guard_name,
                                                    response_text,
                                                )
                                                guardrail_triggered = True
                                                break
                                        if not guardrail_triggered:
                                            if len(conversation_history) >= 200:
                                                conversation_history.pop(0)
                                            conversation_history.append(
                                                {
                                                    "role": "assistant",
                                                    "text": response_text,
                                                    "timestamp": time.time(),
                                                }
                                            )
                                            if len(transcript_entries) >= 200:
                                                transcript_entries.pop(0)
                                            transcript_entries.append(
                                                {"role": "assistant", "text": response_text}
                                            )
                                elif ev_type == "interruption":
                                    # Barge-in: clear Twilio audio buffer
                                    await websocket.send_text(
                                        json.dumps(
                                            {"event": "clear", "streamSid": stream_sid}
                                        )
                                    )
                        except Exception as exc:
                            logger.exception("ElevenLabs ConvAI forward error: %s", exc)

                    receive_task = asyncio.create_task(forward_elevenlabs_to_twilio())

                else:
                    # ---- OpenAI Realtime mode ----
                    from patter.providers.openai_realtime import OpenAIRealtimeAdapter  # type: ignore[import]
                    from patter.services.tool_executor import ToolExecutor  # type: ignore[import]

                    # Build tools list for OpenAI — always include the system transfer_call and end_call tools
                    agent_tools: list[dict] = [
                        {
                            "name": t["name"],
                            "description": t.get("description", ""),
                            "parameters": t.get("parameters", {}),
                        }
                        for t in (agent.tools or [])
                    ]
                    openai_tools: list[dict] = agent_tools + [_TRANSFER_CALL_TOOL, _END_CALL_TOOL]

                    openai_adapter = OpenAIRealtimeAdapter(
                        api_key=openai_key,
                        model=agent.model,
                        voice=agent.voice,
                        instructions=resolved_prompt,
                        language=agent.language,
                        tools=openai_tools,
                    )
                    await openai_adapter.connect()
                    logger.info("OpenAI Realtime connected")

                    # If agent has a first_message, send it immediately
                    if agent.first_message:
                        await openai_adapter.send_text(agent.first_message)

                    # Forward OpenAI responses back to Twilio
                    async def forward_to_twilio() -> None:
                        nonlocal chunk_count
                        tool_executor = ToolExecutor()
                        try:
                            async for event_type, event_data in openai_adapter.receive_events():
                                if event_type == "audio":
                                    encoded = base64.b64encode(event_data).decode("ascii")
                                    await websocket.send_text(
                                        json.dumps(
                                            {
                                                "event": "media",
                                                "streamSid": stream_sid,
                                                "media": {"payload": encoded},
                                            }
                                        )
                                    )
                                    # Send mark so we know when this chunk was played
                                    chunk_count += 1
                                    mark_name = f"audio_{chunk_count}"
                                    await websocket.send_text(
                                        json.dumps(
                                            {
                                                "event": "mark",
                                                "streamSid": stream_sid,
                                                "mark": {"name": mark_name},
                                            }
                                        )
                                    )
                                elif event_type == "transcript_input":
                                    logger.info("User: %s", event_data)
                                    if len(conversation_history) >= 200:
                                        conversation_history.pop(0)
                                    conversation_history.append(
                                        {
                                            "role": "user",
                                            "text": event_data,
                                            "timestamp": time.time(),
                                        }
                                    )
                                    if len(transcript_entries) >= 200:
                                        transcript_entries.pop(0)
                                    transcript_entries.append(
                                        {"role": "user", "text": event_data}
                                    )
                                    if on_transcript:
                                        await on_transcript(
                                            {
                                                "role": "user",
                                                "text": event_data,
                                                "call_id": call_sid_actual,
                                                "history": list(conversation_history),
                                            }
                                        )
                                elif event_type == "transcript_output":
                                    if event_data:
                                        response_text: str = event_data
                                        # --- Apply output guardrails ---
                                        guardrails = getattr(agent, "guardrails", None) or []
                                        guardrail_triggered = False
                                        for guard in guardrails:
                                            blocked = False
                                            blocked_terms = guard.get("blocked_terms") if isinstance(guard, dict) else getattr(guard, "blocked_terms", None)
                                            check_fn = guard.get("check") if isinstance(guard, dict) else getattr(guard, "check", None)
                                            guard_name = guard.get("name") if isinstance(guard, dict) else getattr(guard, "name", "unnamed")
                                            replacement = (guard.get("replacement") if isinstance(guard, dict) else getattr(guard, "replacement", None)) or "I'm sorry, I can't respond to that."
                                            if blocked_terms:
                                                blocked = any(
                                                    term.lower() in response_text.lower()
                                                    for term in blocked_terms
                                                )
                                            if check_fn and not blocked:
                                                try:
                                                    blocked = bool(check_fn(response_text))
                                                except Exception as _guard_exc:
                                                    logger.warning("Guardrail '%s' check error: %s", guard_name, _guard_exc)
                                            if blocked:
                                                logger.warning(
                                                    "Guardrail '%s' triggered on: %.50s",
                                                    guard_name,
                                                    response_text,
                                                )
                                                await openai_adapter.cancel_response()
                                                await openai_adapter.send_text(replacement)
                                                guardrail_triggered = True
                                                break
                                        if not guardrail_triggered:
                                            if len(conversation_history) >= 200:
                                                conversation_history.pop(0)
                                            conversation_history.append(
                                                {
                                                    "role": "assistant",
                                                    "text": response_text,
                                                    "timestamp": time.time(),
                                                }
                                            )
                                            if len(transcript_entries) >= 200:
                                                transcript_entries.pop(0)
                                            transcript_entries.append(
                                                {"role": "assistant", "text": response_text}
                                            )
                                elif event_type == "speech_started":
                                    # Barge-in: clear Twilio audio buffer and cancel response
                                    await websocket.send_text(
                                        json.dumps(
                                            {"event": "clear", "streamSid": stream_sid}
                                        )
                                    )
                                    await openai_adapter.cancel_response()
                                elif event_type == "function_call":
                                    func_data = event_data
                                    if func_data["name"] == "transfer_call":
                                        # System tool — transfer the call
                                        raw_args = func_data.get("arguments", "{}")
                                        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                                        transfer_number = args.get("number", "")
                                        if not _validate_e164(transfer_number):
                                            logger.warning("transfer_call rejected: invalid number %r", transfer_number)
                                            await openai_adapter.send_function_result(
                                                func_data["call_id"],
                                                json.dumps({"error": "Invalid phone number format", "status": "rejected"}),
                                            )
                                            continue
                                        logger.info("Transferring call to %s", transfer_number)
                                        await openai_adapter.send_function_result(
                                            func_data["call_id"],
                                            json.dumps({"status": "transferring", "to": transfer_number}),
                                        )
                                        if twilio_sid and twilio_token and call_sid_actual:
                                            if not _validate_twilio_sid(call_sid_actual, "CA"):
                                                logger.warning(
                                                    "transfer_call skipped: invalid CallSid format %r",
                                                    call_sid_actual,
                                                )
                                            else:
                                                import httpx as _httpx

                                                async with _httpx.AsyncClient() as _http:
                                                    twiml = f"<Response><Dial>{_xml_escape(transfer_number)}</Dial></Response>"
                                                    await _http.post(
                                                        f"https://api.twilio.com/2010-04-01/Accounts/{twilio_sid}/Calls/{call_sid_actual}.json",
                                                        auth=(twilio_sid, twilio_token),
                                                        data={"Twiml": twiml},
                                                    )
                                                logger.info("Call transferred to %s", transfer_number)
                                        if on_transcript:
                                            await on_transcript(
                                                {
                                                    "role": "system",
                                                    "text": f"Call transferred to {transfer_number}",
                                                    "call_id": call_sid_actual,
                                                }
                                            )
                                        return  # Exit forward_to_twilio loop
                                    elif func_data["name"] == "end_call":
                                        raw_args = func_data.get("arguments", "{}")
                                        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                                        reason = args.get("reason", "conversation_complete")
                                        logger.info("Ending call: %s", reason)
                                        await openai_adapter.send_function_result(
                                            func_data["call_id"],
                                            json.dumps({"status": "ending", "reason": reason}),
                                        )
                                        if twilio_sid and twilio_token and call_sid_actual:
                                            if not _validate_twilio_sid(call_sid_actual, "CA"):
                                                logger.warning(
                                                    "end_call skipped: invalid CallSid format %r",
                                                    call_sid_actual,
                                                )
                                            else:
                                                import httpx as _httpx

                                                async with _httpx.AsyncClient() as _http:
                                                    await _http.post(
                                                        f"https://api.twilio.com/2010-04-01/Accounts/{twilio_sid}/Calls/{call_sid_actual}.json",
                                                        auth=(twilio_sid, twilio_token),
                                                        data={"Status": "completed"},
                                                    )
                                        if on_transcript:
                                            await on_transcript(
                                                {
                                                    "role": "system",
                                                    "text": f"Call ended: {reason}",
                                                    "call_id": call_sid_actual,
                                                }
                                            )
                                        return  # Exit forward_to_twilio loop
                                    else:
                                        tool_def = next(
                                            (
                                                t
                                                for t in (agent.tools or [])
                                                if t["name"] == func_data["name"]
                                            ),
                                            None,
                                        )
                                        if tool_def and tool_def.get("webhook_url"):
                                            args = func_data.get("arguments", "{}")
                                            if isinstance(args, str):
                                                args = json.loads(args)
                                            result = await tool_executor.execute(
                                                tool_name=func_data["name"],
                                                arguments=args,
                                                webhook_url=tool_def["webhook_url"],
                                                call_context={
                                                    "call_id": call_sid_actual,
                                                    "caller": caller,
                                                    "callee": callee,
                                                },
                                            )
                                            await openai_adapter.send_function_result(
                                                func_data["call_id"], result
                                            )
                        except Exception as exc:
                            logger.exception("Forward error: %s", exc)

                    receive_task = asyncio.create_task(forward_to_twilio())

            elif event == "media":
                payload = data.get("media", {}).get("payload", "")
                mulaw_audio = base64.b64decode(payload)

                provider = getattr(agent, "provider", "openai_realtime")
                if provider == "pipeline" and stt is not None and not is_speaking:
                    # Send mulaw directly to Deepgram (native mulaw support)
                    await stt.send_audio(mulaw_audio)
                elif provider == "elevenlabs_convai" and elevenlabs_adapter is not None:
                    # ElevenLabs expects raw audio bytes
                    await elevenlabs_adapter.send_audio(mulaw_audio)
                elif openai_adapter is not None:
                    await openai_adapter.send_audio(mulaw_audio)

            elif event == "mark":
                mark_name = data.get("mark", {}).get("name", "")
                last_confirmed_mark = mark_name

            elif event == "dtmf":
                digit = data.get("dtmf", {}).get("digit", "")
                logger.info("DTMF: %s", digit)
                if openai_adapter is not None:
                    await openai_adapter.send_text(
                        f"The user pressed key {digit} on their phone keypad."
                    )
                if on_transcript:
                    await on_transcript(
                        {
                            "role": "user",
                            "text": f"[DTMF: {digit}]",
                            "call_id": call_sid_actual,
                        }
                    )

            elif event == "stop":
                break

    except Exception as exc:
        logger.exception("Stream error: %s", exc)
    finally:
        if stt_task:
            stt_task.cancel()
        if receive_task:
            receive_task.cancel()
        if stt is not None:
            await stt.close()
        if tts is not None:
            await tts.close()
        if openai_adapter:
            await openai_adapter.close()
        if elevenlabs_adapter:
            await elevenlabs_adapter.close()
        if on_call_end:
            try:
                await on_call_end(
                    {
                        "call_id": call_sid_actual,
                        "transcript": list(conversation_history),
                    }
                )
            except Exception as exc:
                logger.exception("on_call_end error: %s", exc)
        logger.info("Call ended")
