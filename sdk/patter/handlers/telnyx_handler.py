"""Telnyx webhook and stream handlers for local mode."""

from __future__ import annotations

import base64
import json
import logging
import time
from collections import deque
from urllib.parse import quote

from patter.handlers.stream_handler import (
    AudioSender,
    ElevenLabsConvAIStreamHandler,
    OpenAIRealtimeStreamHandler,
    PipelineStreamHandler,
    apply_call_overrides,
    create_metrics_accumulator,
    fetch_deepgram_cost,
    resolve_agent_prompt,
)

logger = logging.getLogger("patter")

# Maximum size (bytes) of a single WebSocket message accepted from Telnyx.
# Telnyx 16 kHz PCM frames are ~640 bytes (20 ms).  1 MB defends against
# memory exhaustion from a malformed or malicious stream peer.
_MAX_WS_MESSAGE_BYTES = 1 * 1024 * 1024


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


# ---------------------------------------------------------------------------
# Telnyx AudioSender — no transcoding needed (16 kHz PCM native)
# ---------------------------------------------------------------------------

class TelnyxAudioSender(AudioSender):
    """Sends audio to a Telnyx WebSocket (16 kHz PCM, no transcoding)."""

    def __init__(self, websocket) -> None:
        self._ws = websocket

    async def send_audio(self, pcm_audio: bytes) -> None:
        encoded = base64.b64encode(pcm_audio).decode("ascii")
        await self._ws.send_text(
            json.dumps(
                {
                    "event_type": "media",
                    "payload": {"audio": {"chunk": encoded}},
                }
            )
        )

    async def send_clear(self) -> None:
        await self._ws.send_text(
            json.dumps({"event_type": "media_stop"})
        )

    async def send_mark(self, mark_name: str) -> None:
        # Telnyx does not support playback marks — no-op
        pass


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
    telnyx_key: str = "",
    on_metrics=None,
    pricing: dict | None = None,
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
    transcript_entries: deque[dict] = deque(maxlen=200)
    stream_started = False

    handler: OpenAIRealtimeStreamHandler | ElevenLabsConvAIStreamHandler | PipelineStreamHandler | None = None
    metrics = None

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

                # Fire on_call_start callback — may return per-call config overrides
                _call_overrides = None
                if on_call_start:
                    _call_overrides = await on_call_start(
                        {
                            "call_id": call_id_actual,
                            "caller": caller,
                            "callee": callee,
                            "direction": "inbound",
                        }
                    )
                    if not isinstance(_call_overrides, dict):
                        _call_overrides = None

                # Apply per-call overrides (dynamic agent config)
                if _call_overrides:
                    agent = apply_call_overrides(agent, _call_overrides)

                # Resolve dynamic variables in system prompt
                resolved_prompt = resolve_agent_prompt(agent)
                provider = getattr(agent, "provider", "openai_realtime")

                # Initialize metrics
                metrics = create_metrics_accumulator(
                    call_id=call_id_actual,
                    provider=provider,
                    telephony_provider="telnyx",
                    agent=agent,
                    deepgram_key=deepgram_key,
                    elevenlabs_key=elevenlabs_key,
                    pricing=pricing,
                )
                # Telnyx uses PCM 16kHz (2 bytes/sample)
                metrics.configure_stt_format(sample_rate=16000, bytes_per_sample=2)

                # Create audio sender
                audio_sender = TelnyxAudioSender(websocket)

                # --- Telnyx-specific call control helpers ---
                async def _telnyx_transfer(number):
                    if telnyx_key and call_id_actual:
                        import httpx as _httpx
                        async with _httpx.AsyncClient() as _http:
                            await _http.post(
                                f"https://api.telnyx.com/v2/calls/{call_id_actual}/actions/transfer",
                                headers={"Authorization": f"Bearer {telnyx_key}"},
                                json={"to": number},
                                timeout=10.0,
                            )
                        logger.info("Telnyx call transferred to %s", number)

                async def _telnyx_hangup():
                    if telnyx_key and call_id_actual:
                        import httpx as _httpx
                        async with _httpx.AsyncClient() as _http:
                            await _http.post(
                                f"https://api.telnyx.com/v2/calls/{call_id_actual}/actions/hangup",
                                headers={"Authorization": f"Bearer {telnyx_key}"},
                                json={},
                                timeout=10.0,
                            )
                        logger.info("Telnyx call hung up")

                # Create the appropriate stream handler
                if provider == "pipeline":
                    handler = PipelineStreamHandler(
                        agent=agent,
                        audio_sender=audio_sender,
                        call_id=call_id_actual,
                        caller=caller,
                        callee=callee,
                        resolved_prompt=resolved_prompt,
                        metrics=metrics,
                        openai_key=openai_key,
                        deepgram_key=deepgram_key,
                        elevenlabs_key=elevenlabs_key,
                        for_twilio=False,
                        transfer_fn=_telnyx_transfer,
                        hangup_fn=_telnyx_hangup,
                        on_transcript=on_transcript,
                        on_message=on_message,
                        on_metrics=on_metrics,
                        transcript_entries=transcript_entries,
                    )
                elif provider == "elevenlabs_convai":
                    handler = ElevenLabsConvAIStreamHandler(
                        agent=agent,
                        audio_sender=audio_sender,
                        call_id=call_id_actual,
                        caller=caller,
                        callee=callee,
                        resolved_prompt=resolved_prompt,
                        metrics=metrics,
                        elevenlabs_key=elevenlabs_key,
                        on_transcript=on_transcript,
                        on_metrics=on_metrics,
                        transcript_entries=transcript_entries,
                    )
                else:
                    handler = OpenAIRealtimeStreamHandler(
                        agent=agent,
                        audio_sender=audio_sender,
                        call_id=call_id_actual,
                        caller=caller,
                        callee=callee,
                        resolved_prompt=resolved_prompt,
                        metrics=metrics,
                        openai_key=openai_key,
                        transfer_fn=_telnyx_transfer,
                        hangup_fn=_telnyx_hangup,
                        on_transcript=on_transcript,
                        on_metrics=on_metrics,
                        transcript_entries=transcript_entries,
                        audio_format="pcm16",
                    )

                await handler.start()

            elif event_type_telnyx == "media":
                payload_data = data.get("payload", {})
                audio_chunk_b64 = payload_data.get("audio", {}).get("chunk", "")
                if not audio_chunk_b64:
                    continue

                pcm_audio = base64.b64decode(audio_chunk_b64)
                if handler is not None:
                    await handler.on_audio_received(pcm_audio)

            elif event_type_telnyx == "stream_stopped":
                break

    except Exception as exc:
        logger.exception("Stream error: %s", exc)
    finally:
        if handler is not None:
            await handler.cleanup()

        # --- Metrics: query actual telephony cost from Telnyx ---
        if metrics is not None and telnyx_key and call_id_actual:
            try:
                import httpx as _httpx

                async with _httpx.AsyncClient() as _http:
                    resp = await _http.get(
                        f"https://api.telnyx.com/v2/calls/{call_id_actual}",
                        headers={"Authorization": f"Bearer {telnyx_key}"},
                        timeout=5.0,
                    )
                    if resp.status_code == 200:
                        call_data = resp.json().get("data", {})
                        cost = call_data.get("cost", {})
                        total_cost = cost.get("amount")
                        if total_cost is not None:
                            metrics.set_actual_telephony_cost(abs(float(total_cost)))
                            logger.info("Telnyx actual cost: $%s", abs(float(total_cost)))
            except Exception as exc:
                logger.debug("Could not fetch Telnyx call cost: %s", exc)

        # --- Metrics: query actual STT cost from Deepgram ---
        stt = getattr(handler, "stt", None) if handler is not None else None
        await fetch_deepgram_cost(metrics, stt, deepgram_key)

        # --- Metrics: finalize ---
        call_metrics = None
        if metrics is not None:
            try:
                call_metrics = metrics.end_call()
            except Exception as exc:
                logger.warning("Metrics finalization error: %s", exc)
        if on_call_end:
            try:
                await on_call_end(
                    {
                        "call_id": call_id_actual,
                        "caller": caller,
                        "callee": callee,
                        "ended_at": time.time(),
                        "transcript": list(transcript_entries),
                        "metrics": call_metrics,
                    }
                )
            except Exception as exc:
                logger.exception("on_call_end error: %s", exc)
        logger.info("Telnyx call ended")
