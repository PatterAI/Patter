"""Telnyx webhook and stream handlers for local mode."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from urllib.parse import quote

logger = logging.getLogger("patter")

# Maximum size (bytes) of a single WebSocket message accepted from Telnyx.
# Telnyx 16 kHz PCM frames are ~640 bytes (20 ms).  1 MB defends against
# memory exhaustion from a malformed or malicious stream peer.
_MAX_WS_MESSAGE_BYTES = 1 * 1024 * 1024


def _validate_e164(number: str) -> bool:
    """Return True if *number* is a valid E.164 phone number."""
    return bool(re.match(r'^\+[1-9]\d{6,14}$', number))


def _sanitize_variable_value(value: str) -> str:
    """Strip control characters and limit length to prevent prompt injection."""
    return re.sub(r'[\x00-\x1f\x7f]', '', str(value))[:500]


def _resolve_variables(template: str, variables: dict) -> str:
    """Replace ``{key}`` placeholders in *template* with values from *variables*."""
    result = template
    for key, value in variables.items():
        result = result.replace(f"{{{key}}}", str(value))
    return result


def _create_stt_from_config(config, for_twilio: bool = False):
    """Create an STT adapter from an STTConfig object.

    Args:
        config: An ``STTConfig`` instance (or ``None``).
        for_twilio: Ignored for Telnyx (always 16 kHz PCM), kept for API
            symmetry with the Twilio handler.
    """
    if config is None:
        return None
    provider = config.provider
    if provider == "deepgram":
        from patter.providers.deepgram_stt import DeepgramSTT  # type: ignore[import]

        # Telnyx is always 16 kHz PCM linear16
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


def telnyx_webhook_handler(
    call_id: str,
    caller: str,
    callee: str,
    webhook_base_url: str,
    connection_id: str = "",
) -> dict:
    """Generate Telnyx Call Control response for an incoming call.

    Returns a dict that should be serialised to JSON and returned with 200 OK.
    Telnyx Call Control uses a command-based model: the webhook handler responds
    with ``answer`` and then ``stream_start`` commands.

    Args:
        call_id: Telnyx ``call_control_id``.
        caller: The calling number.
        callee: The called number.
        webhook_base_url: Hostname (no scheme) of this server, e.g. "abc.ngrok.io".
        connection_id: Telnyx TeXML App / Call Control App ID (optional).
    """
    stream_url = (
        f"wss://{webhook_base_url}/ws/telnyx/stream/{call_id}"
        f"?caller={quote(caller)}&callee={quote(callee)}"
    )
    # Telnyx Call Control: answer first, then stream_start
    return {
        "commands": [
            {"command": "answer"},
            {
                "command": "stream_start",
                "params": {
                    "stream_url": stream_url,
                    "stream_track": "both_tracks",
                },
            },
        ]
    }


async def telnyx_stream_bridge(
    websocket,
    agent,
    openai_key: str,
    on_call_start=None,
    on_call_end=None,
    on_transcript=None,
    on_message=None,
    deepgram_key: str = "",
    elevenlabs_key: str = "",
) -> None:
    """Bridge a Telnyx WebSocket media stream to the configured AI provider.

    Supports two provider modes depending on ``agent.provider``:

    * ``"openai_realtime"`` (default) — streams 16 kHz PCM directly to the
      OpenAI Realtime API (no transcoding needed on Telnyx).
    * ``"pipeline"`` — uses Deepgram for STT (16 kHz PCM), calls ``on_message``
      with the transcript, then synthesises the response with ElevenLabs TTS
      and sends it back to Telnyx.

    Args:
        websocket: A Starlette/FastAPI WebSocket instance.
        agent: An ``Agent`` dataclass with prompt, voice, model, tools, etc.
        openai_key: OpenAI API key for the Realtime API (openai_realtime mode).
        on_call_start: Optional async callable(dict) — fired when streaming starts.
        on_call_end: Optional async callable(dict) — fired when streaming ends.
        on_transcript: Optional async callable(dict) — fired for each user utterance.
        on_message: Optional async callable(dict) -> str — called with the user's
            text in pipeline mode; return value is synthesised and played back.
        deepgram_key: Deepgram API key (pipeline mode).
        elevenlabs_key: ElevenLabs API key (pipeline mode).
    """
    await websocket.accept()

    caller: str = websocket.query_params.get("caller", "")
    callee: str = websocket.query_params.get("callee", "")

    call_id_actual: str = ""
    transcript_entries: list[dict] = []
    stream_started = False

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

    try:
        while True:
            raw = await websocket.receive_text()
            if len(raw) > _MAX_WS_MESSAGE_BYTES:
                logger.warning(
                    "Oversized Telnyx WebSocket message dropped (%d bytes)", len(raw)
                )
                continue
            data = json.loads(raw)
            event_type_telnyx = data.get("event_type", "")

            # Telnyx uses event_type instead of event, and wraps payload in "payload"
            if event_type_telnyx == "stream_started" and not stream_started:
                stream_started = True
                payload_data = data.get("payload", {})
                call_id_actual = payload_data.get("call_control_id", "")

                logger.info("Telnyx stream started: %s", call_id_actual)

                # Fire on_call_start callback
                if on_call_start:
                    await on_call_start(
                        {
                            "call_id": call_id_actual,
                            "caller": caller,
                            "callee": callee,
                            "direction": "inbound",
                        }
                    )

                # Resolve dynamic variables in system prompt (mirrors Twilio handler behaviour).
                resolved_prompt = agent.system_prompt
                agent_variables: dict = getattr(agent, "variables", None) or {}
                if agent_variables:
                    sanitized_vars = {k: _sanitize_variable_value(v) for k, v in agent_variables.items()}
                    resolved_prompt = _resolve_variables(resolved_prompt, sanitized_vars)

                provider = getattr(agent, "provider", "openai_realtime")

                if provider == "pipeline":
                    # ---- Pipeline mode: configurable STT + TTS ----

                    # Create STT: prefer agent.stt config, fall back to deepgram_key
                    if agent.stt:
                        stt = _create_stt_from_config(agent.stt)
                    elif deepgram_key:
                        from patter.providers.deepgram_stt import DeepgramSTT  # type: ignore[import]

                        # Telnyx sends 16 kHz PCM — use linear16 encoding for Deepgram
                        stt = DeepgramSTT(
                            api_key=deepgram_key,
                            language=agent.language,
                            encoding="linear16",
                            sample_rate=16000,
                        )
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
                        logger.warning("Telnyx pipeline: no STT configured")
                    if tts is None:
                        logger.warning("Telnyx pipeline: no TTS configured")

                    if stt is not None:
                        await stt.connect()

                    logger.info("Pipeline mode (Telnyx): STT + TTS connected")

                    # If agent has a first_message, synthesise and play it now
                    if agent.first_message and on_message is None and tts is not None:
                        async for audio_chunk in tts.synthesize(agent.first_message):
                            encoded = base64.b64encode(audio_chunk).decode("ascii")
                            await websocket.send_text(
                                json.dumps(
                                    {
                                        "event_type": "media",
                                        "payload": {"audio": {"chunk": encoded}},
                                    }
                                )
                            )

                    # STT receive loop
                    async def stt_loop_telnyx() -> None:
                        nonlocal is_speaking
                        try:
                            async for transcript in stt.receive_transcripts():
                                if not (transcript.is_final and transcript.text):
                                    continue

                                logger.info("User: %s", transcript.text)
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
                                            "call_id": call_id_actual,
                                        }
                                    )

                                if on_message is None:
                                    continue

                                response_text = await on_message(
                                    {
                                        "text": transcript.text,
                                        "call_id": call_id_actual,
                                        "caller": caller,
                                    }
                                )

                                if not response_text:
                                    continue

                                if len(transcript_entries) >= 200:
                                    transcript_entries.pop(0)
                                transcript_entries.append(
                                    {"role": "assistant", "text": response_text}
                                )
                                is_speaking = True
                                async for audio_chunk in tts.synthesize(response_text):
                                    if not is_speaking:
                                        break
                                    encoded = base64.b64encode(audio_chunk).decode("ascii")
                                    await websocket.send_text(
                                        json.dumps(
                                            {
                                                "event_type": "media",
                                                "payload": {"audio": {"chunk": encoded}},
                                            }
                                        )
                                    )
                                is_speaking = False
                        except Exception as exc:
                            logger.exception("Telnyx pipeline STT loop error: %s", exc)

                    stt_task = asyncio.create_task(stt_loop_telnyx())

                elif provider == "elevenlabs_convai":
                    # ---- ElevenLabs Conversational AI mode ----
                    from patter.providers.elevenlabs_convai import ElevenLabsConvAIAdapter  # type: ignore[import]

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
                    logger.info("ElevenLabs ConvAI connected (Telnyx)")

                    async def forward_elevenlabs_to_telnyx() -> None:
                        try:
                            async for ev_type, ev_data in elevenlabs_adapter.receive_events():
                                if ev_type == "audio":
                                    # Telnyx uses 16 kHz PCM — ElevenLabs returns PCM, no transcoding needed
                                    encoded = base64.b64encode(ev_data).decode("ascii")
                                    await websocket.send_text(
                                        json.dumps(
                                            {
                                                "event_type": "media",
                                                "payload": {"audio": {"chunk": encoded}},
                                            }
                                        )
                                    )
                                elif ev_type == "transcript_input":
                                    logger.info("User: %s", ev_data)
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
                                                "call_id": call_id_actual,
                                            }
                                        )
                                elif ev_type == "transcript_output":
                                    if ev_data:
                                        if len(transcript_entries) >= 200:
                                            transcript_entries.pop(0)
                                        transcript_entries.append(
                                            {"role": "assistant", "text": ev_data}
                                        )
                                elif ev_type == "interruption":
                                    # Barge-in: stop Telnyx playback
                                    await websocket.send_text(
                                        json.dumps({"event_type": "media_stop"})
                                    )
                        except Exception as exc:
                            logger.exception("ElevenLabs ConvAI forward error (Telnyx): %s", exc)

                    receive_task = asyncio.create_task(forward_elevenlabs_to_telnyx())

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
                        audio_format="pcm16",
                    )
                    await openai_adapter.connect()
                    logger.info("OpenAI Realtime connected (Telnyx)")

                    # If agent has a first_message, send it immediately
                    if agent.first_message:
                        await openai_adapter.send_text(agent.first_message)

                    # Forward OpenAI responses back to Telnyx
                    async def forward_to_telnyx() -> None:
                        tool_executor = ToolExecutor()
                        try:
                            async for ev_type, ev_data in openai_adapter.receive_events():
                                if ev_type == "audio":
                                    # Telnyx expects base64-encoded PCM audio
                                    encoded = base64.b64encode(ev_data).decode("ascii")
                                    await websocket.send_text(
                                        json.dumps(
                                            {
                                                "event_type": "media",
                                                "payload": {"audio": {"chunk": encoded}},
                                            }
                                        )
                                    )
                                elif ev_type == "transcript_input":
                                    logger.info("User: %s", ev_data)
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
                                                "call_id": call_id_actual,
                                            }
                                        )
                                elif ev_type == "transcript_output":
                                    if ev_data:
                                        if len(transcript_entries) >= 200:
                                            transcript_entries.pop(0)
                                        transcript_entries.append(
                                            {"role": "assistant", "text": ev_data}
                                        )
                                elif ev_type == "speech_started":
                                    # Barge-in: stop playback and cancel response
                                    await websocket.send_text(
                                        json.dumps({"event_type": "media_stop"})
                                    )
                                    await openai_adapter.cancel_response()
                                elif ev_type == "function_call":
                                    func_data = ev_data
                                    if func_data["name"] == "transfer_call":
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
                                        logger.info("Transferring Telnyx call to %s", transfer_number)
                                        await openai_adapter.send_function_result(
                                            func_data["call_id"],
                                            json.dumps({"status": "transferring", "to": transfer_number}),
                                        )
                                        if on_transcript:
                                            await on_transcript(
                                                {
                                                    "role": "system",
                                                    "text": f"Call transferred to {transfer_number}",
                                                    "call_id": call_id_actual,
                                                }
                                            )
                                        return  # Exit forward_to_telnyx loop
                                    elif func_data["name"] == "end_call":
                                        raw_args = func_data.get("arguments", "{}")
                                        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                                        reason = args.get("reason", "conversation_complete")
                                        logger.info("Ending Telnyx call: %s", reason)
                                        await openai_adapter.send_function_result(
                                            func_data["call_id"],
                                            json.dumps({"status": "ending", "reason": reason}),
                                        )
                                        if on_transcript:
                                            await on_transcript(
                                                {
                                                    "role": "system",
                                                    "text": f"Call ended: {reason}",
                                                    "call_id": call_id_actual,
                                                }
                                            )
                                        return  # Exit forward_to_telnyx loop
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
                                                    "call_id": call_id_actual,
                                                    "caller": caller,
                                                    "callee": callee,
                                                },
                                            )
                                            await openai_adapter.send_function_result(
                                                func_data["call_id"], result
                                            )
                        except Exception as exc:
                            logger.exception("Telnyx forward error: %s", exc)

                    receive_task = asyncio.create_task(forward_to_telnyx())

            elif event_type_telnyx == "media":
                payload_data = data.get("payload", {})
                audio_chunk_b64 = payload_data.get("audio", {}).get("chunk", "")
                if not audio_chunk_b64:
                    continue

                pcm_audio = base64.b64decode(audio_chunk_b64)
                provider = getattr(agent, "provider", "openai_realtime")

                if provider == "pipeline" and stt is not None and not is_speaking:
                    # Telnyx 16 kHz PCM — send directly to Deepgram
                    await stt.send_audio(pcm_audio)
                elif provider == "elevenlabs_convai" and elevenlabs_adapter is not None:
                    await elevenlabs_adapter.send_audio(pcm_audio)
                elif openai_adapter is not None:
                    await openai_adapter.send_audio(pcm_audio)

            elif event_type_telnyx == "stream_stopped":
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
                        "call_id": call_id_actual,
                        "transcript": transcript_entries,
                    }
                )
            except Exception as exc:
                logger.exception("on_call_end error: %s", exc)
        logger.info("Telnyx call ended")
