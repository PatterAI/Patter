"""Telnyx webhook and stream handlers for local mode."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from collections import deque
from urllib.parse import quote

from getpatter.handlers.common import _validate_e164
from getpatter.utils.log_sanitize import mask_phone_number
from getpatter.handlers.stream_handler import (
    AudioSender,
    ElevenLabsConvAIStreamHandler,
    OpenAIRealtimeStreamHandler,
    PipelineStreamHandler,
    apply_call_overrides,
    create_metrics_accumulator,
    fetch_deepgram_cost,
    resolve_agent_prompt,
)

# DTMF digits accepted by the Telnyx ``send_dtmf`` command. Duration bounds
# mirror Telnyx API constraints (100–500 ms per digit).
_DTMF_ALLOWED = frozenset("0123456789*#ABCDabcd")
_DTMF_DEFAULT_DURATION_MS = 250

# SIP URI validator — very permissive: ``sip:`` or ``sips:`` scheme plus a
# non-empty host component, per Telnyx transfer semantics. E.164 numbers
# are checked separately via ``_validate_e164``.
_SIP_URI_RE = re.compile(r"^sips?:[^\s@]+(@[^\s]+)?$", re.IGNORECASE)


def _is_valid_transfer_target(target: str) -> bool:
    """Accept either a validated E.164 phone number or a SIP(s) URI."""
    if not isinstance(target, str) or not target:
        return False
    if _validate_e164(target):
        return True
    return bool(_SIP_URI_RE.match(target))

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
    """Sends audio to a Telnyx media-stream WebSocket.

    Telnyx expects outbound frames in the same codec negotiated at
    ``streaming_start`` time (see BUG #16). The server currently negotiates
    PCMU 8 kHz bidirectional, so OpenAI Realtime is configured to emit
    ``g711_ulaw`` directly — the sender forwards the bytes as-is. For
    pipeline mode, ``input_is_mulaw_8k=False`` keeps the PCM16 16 kHz path
    (Telnyx transcodes on the RTP leg when negotiated as L16/16000).

    Wire format (BUG #18): ``{"event": "media", "media": {"payload": b64}}``.
    """

    def __init__(self, websocket, input_is_mulaw_8k: bool = False) -> None:
        self._ws = websocket
        self._input_is_mulaw_8k = input_is_mulaw_8k
        # Lazy import transcoding when the caller sends PCM16 16 kHz and
        # we need to match the negotiated PCMU 8 kHz bidirectional stream.
        if not input_is_mulaw_8k:
            from getpatter.services.transcoding import pcm16_to_mulaw, resample_16k_to_8k  # type: ignore[import]
            self._pcm16_to_mulaw = pcm16_to_mulaw
            self._resample_16k_to_8k = resample_16k_to_8k
        else:
            self._pcm16_to_mulaw = None
            self._resample_16k_to_8k = None

    async def send_audio(self, audio: bytes) -> None:
        if self._input_is_mulaw_8k:
            mulaw = audio
        else:
            resampled = self._resample_16k_to_8k(audio)
            mulaw = self._pcm16_to_mulaw(resampled)
        encoded = base64.b64encode(mulaw).decode("ascii")
        await self._ws.send_text(
            json.dumps({"event": "media", "media": {"payload": encoded}})
        )

    async def send_clear(self) -> None:
        # Telnyx media stream clear signal. See BUG #18.
        await self._ws.send_text(json.dumps({"event": "clear"}))

    async def send_mark(self, mark_name: str) -> None:
        # Telnyx media streams do not support playback marks — no-op.
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
    recording: bool = False,
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
            # Telnyx media-stream WebSocket uses ``event`` (not
            # ``event_type``, which is a Call Control REST notification
            # field). See BUG #17.
            event_type_telnyx = data.get("event", "")

            if event_type_telnyx == "connected":
                # First frame after WS open — Telnyx ping. Nothing to do.
                continue

            if event_type_telnyx == "start" and not stream_started:
                stream_started = True
                start_info = data.get("start", {}) or {}
                call_id_actual = start_info.get("call_control_id", "")
                caller = start_info.get("from", "") or caller
                callee = start_info.get("to", "") or callee

                # Single INFO line per call-start — full context in one place.
                _mode = (
                    f"engine={getattr(agent.engine, 'kind', 'unknown')}"
                    if getattr(agent, 'engine', None) is not None
                    else "pipeline"
                    if (getattr(agent, 'stt', None) is not None and getattr(agent, 'tts', None) is not None)
                    else f"engine={getattr(agent, 'provider', 'unknown')}"
                )
                logger.info(
                    "Call started: %s (Telnyx, %s, %s → %s)",
                    call_id_actual, _mode, caller or '?', callee or '?',
                )

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

                # Create audio sender. OpenAI Realtime negotiates g711_ulaw
                # 8 kHz to match the `streaming_start` PCMU bidirectional
                # stream — forward bytes as-is. Pipeline and ConvAI still
                # produce PCM16 that Telnyx accepts when L16 is negotiated.
                _input_is_mulaw = getattr(agent, "provider", "openai_realtime") == "openai_realtime"
                audio_sender = TelnyxAudioSender(websocket, input_is_mulaw_8k=_input_is_mulaw)

                # --- Telnyx-specific call control helpers ---
                async def _telnyx_transfer(number):
                    """Blind-transfer the call via the Telnyx Call Control API.

                    Accepts either an E.164 phone number or a SIP URI
                    (``sip:user@host`` / ``sips:user@host``).
                    """
                    if not _is_valid_transfer_target(number):
                        logger.warning(
                            "Telnyx transfer rejected: invalid target %s",
                            mask_phone_number(number),
                        )
                        return
                    if telnyx_key and call_id_actual:
                        import httpx as _httpx
                        async with _httpx.AsyncClient() as _http:
                            await _http.post(
                                f"https://api.telnyx.com/v2/calls/{call_id_actual}/actions/transfer",
                                headers={"Authorization": f"Bearer {telnyx_key}"},
                                json={"to": number},
                                timeout=10.0,
                            )
                        logger.debug(
                            "Telnyx call transferred to %s", mask_phone_number(number)
                        )

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
                        logger.debug("Telnyx call hung up")

                async def _telnyx_send_dtmf(digits: str, delay_ms: int = 300) -> None:
                    """Send DTMF digits via the Telnyx Call Control API.

                    Emits one ``send_dtmf`` command per digit, sleeping
                    ``delay_ms`` milliseconds between digits. Telnyx's
                    ``duration_millis`` is clamped to 100–500 ms per digit.
                    """
                    if not digits:
                        logger.warning("Telnyx send_dtmf called with empty digits")
                        return
                    if not (telnyx_key and call_id_actual):
                        logger.warning(
                            "Telnyx send_dtmf skipped: telnyx_key or call_id missing"
                        )
                        return

                    filtered = [d for d in digits if d in _DTMF_ALLOWED]
                    if not filtered:
                        logger.warning(
                            "Telnyx send_dtmf: no valid digits in %r", digits
                        )
                        return

                    duration = max(100, min(500, _DTMF_DEFAULT_DURATION_MS))
                    import httpx as _httpx

                    async with _httpx.AsyncClient() as _http:
                        for idx, digit in enumerate(filtered):
                            await _http.post(
                                f"https://api.telnyx.com/v2/calls/{call_id_actual}/actions/send_dtmf",
                                headers={"Authorization": f"Bearer {telnyx_key}"},
                                json={
                                    "digits": digit,
                                    "duration_millis": duration,
                                },
                                timeout=10.0,
                            )
                            if idx < len(filtered) - 1 and delay_ms > 0:
                                await asyncio.sleep(delay_ms / 1000)
                    logger.debug(
                        "Telnyx DTMF sent (%d digits, delay=%dms)",
                        len(filtered),
                        delay_ms,
                    )

                async def _telnyx_start_recording() -> None:
                    """Start recording the call via Telnyx Call Control API."""
                    if not (telnyx_key and call_id_actual):
                        return
                    import httpx as _httpx

                    async with _httpx.AsyncClient() as _http:
                        resp = await _http.post(
                            f"https://api.telnyx.com/v2/calls/{call_id_actual}/actions/record_start",
                            headers={"Authorization": f"Bearer {telnyx_key}"},
                            json={"format": "mp3", "channels": "single"},
                            timeout=10.0,
                        )
                        if resp.status_code >= 400:
                            logger.warning(
                                "Telnyx record_start failed (%d): %s",
                                resp.status_code,
                                resp.text[:200],
                            )
                        else:
                            logger.debug("Telnyx recording started")

                async def _telnyx_stop_recording() -> None:
                    """Stop recording the call via Telnyx Call Control API."""
                    if not (telnyx_key and call_id_actual):
                        return
                    import httpx as _httpx

                    async with _httpx.AsyncClient() as _http:
                        resp = await _http.post(
                            f"https://api.telnyx.com/v2/calls/{call_id_actual}/actions/record_stop",
                            headers={"Authorization": f"Bearer {telnyx_key}"},
                            json={},
                            timeout=10.0,
                        )
                        if resp.status_code >= 400:
                            logger.warning(
                                "Telnyx record_stop failed (%d): %s",
                                resp.status_code,
                                resp.text[:200],
                            )
                        else:
                            logger.debug("Telnyx recording stopped")

                # Kick off recording if requested
                if recording:
                    try:
                        await _telnyx_start_recording()
                    except Exception as _exc:
                        logger.warning("Could not start recording: %s", _exc)

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
                        # Telnyx bidirectional PCMU 8 kHz: inbound caller
                        # audio arrives as mulaw 8 kHz and must be
                        # transcoded to PCM16 16 kHz before STT. Outbound
                        # PCM16 16 kHz from the TTS must be transcoded
                        # back to mulaw 8 kHz in the audio_sender.
                        input_is_mulaw_8k=True,
                        output_is_mulaw_8k=False,  # TTS produces PCM16; sender transcodes
                        transfer_fn=_telnyx_transfer,
                        hangup_fn=_telnyx_hangup,
                        send_dtmf_fn=_telnyx_send_dtmf,
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
                        # Telnyx Call Control media streams deliver PCMU
                        # 8 kHz (g711 mulaw) in both directions when
                        # streaming_start negotiates PCMU bidirectional.
                        # Verified 2026-04-21: inbound chunks are 160 B /
                        # 20 ms → PCMU 8 kHz. OpenAI Realtime with this
                        # codec forwards bytes pass-through on both legs.
                        audio_format="g711_ulaw",
                    )

                await handler.start()

            elif event_type_telnyx == "media":
                media = data.get("media", {}) or {}
                # Telnyx with ``stream_track=both_tracks`` emits media for
                # both the caller leg (``track=inbound``) and the leg we
                # are injecting audio into (``track=outbound``). Forwarding
                # the ``outbound`` echo to OpenAI Realtime would feed the
                # agent its own voice and break turn detection — only the
                # inbound track is actual caller audio.
                track = media.get("track", "inbound")
                if track != "inbound":
                    continue
                audio_chunk_b64 = media.get("payload", "")
                if not audio_chunk_b64:
                    continue

                pcm_audio = base64.b64decode(audio_chunk_b64)
                if handler is not None:
                    await handler.on_audio_received(pcm_audio)

            elif event_type_telnyx == "dtmf":
                dtmf_info = data.get("dtmf", {}) or {}
                digit = str(dtmf_info.get("digit", "")).strip()
                if digit:
                    logger.debug("Telnyx DTMF received: %s", digit)
                    if handler is not None:
                        try:
                            on_dtmf = getattr(handler, "on_dtmf", None)
                            if callable(on_dtmf):
                                await on_dtmf(digit)
                        except Exception as _exc:
                            logger.debug("on_dtmf handler error: %s", _exc)
                    if on_transcript:
                        try:
                            await on_transcript(
                                {
                                    "role": "user",
                                    "text": f"[DTMF: {digit}]",
                                    "call_id": call_id_actual,
                                }
                            )
                        except Exception as _exc:
                            logger.debug("on_transcript DTMF dispatch error: %s", _exc)

            elif event_type_telnyx == "error":
                logger.warning("Telnyx stream error: %s", data.get("payload") or data)

            elif event_type_telnyx == "stop":
                break

    except Exception as exc:
        logger.exception("Stream error: %s", exc)
    finally:
        # Best-effort recording stop — only if recording was requested and
        # the call is still active. Telnyx auto-stops on hangup so errors
        # are non-fatal.
        if recording and telnyx_key and call_id_actual:
            try:
                import httpx as _httpx

                async with _httpx.AsyncClient() as _http:
                    await _http.post(
                        f"https://api.telnyx.com/v2/calls/{call_id_actual}/actions/record_stop",
                        headers={"Authorization": f"Bearer {telnyx_key}"},
                        json={},
                        timeout=5.0,
                    )
            except Exception as _exc:
                logger.debug("Telnyx record_stop best-effort failed: %s", _exc)

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
                            logger.debug("Telnyx actual cost: $%s", abs(float(total_cost)))
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

        # Single INFO line per call-end — duration, turns, cost, latency.
        if call_metrics is not None:
            _dur = getattr(call_metrics, 'duration_seconds', 0) or 0
            _turns = len(getattr(call_metrics, 'turns', []) or [])
            _cost = getattr(getattr(call_metrics, 'cost', None), 'total', 0) or 0
            _p95 = getattr(getattr(call_metrics, 'latency_p95', None), 'total_ms', 0) or 0
            logger.info(
                "Call ended: %s (%.1fs, %d turns, cost=$%.4f, p95=%dms)",
                call_id_actual, _dur, _turns, _cost, round(_p95),
            )
        else:
            logger.info("Call ended: %s", call_id_actual)
