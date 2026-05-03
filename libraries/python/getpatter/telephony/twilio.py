"""Twilio webhook and stream handlers for local mode."""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from collections import deque
from urllib.parse import quote

from getpatter.telephony.common import (
    _create_stt_from_config,  # noqa: F401 — re-exported for tests and external callers
    _create_tts_from_config,  # noqa: F401 — re-exported for tests and external callers
    _resolve_variables,  # noqa: F401 — re-exported for tests and external callers
    _sanitize_variable_value,  # noqa: F401 — re-exported for tests and external callers
    _validate_e164,
)
from getpatter.utils.log_sanitize import mask_phone_number
from getpatter.stream_handler import (
    END_CALL_TOOL,
    TRANSFER_CALL_TOOL,
    AudioSender,
    ElevenLabsConvAIStreamHandler,
    OpenAIRealtimeStreamHandler,
    PipelineStreamHandler,
    apply_call_overrides,
    create_metrics_accumulator,
    fetch_deepgram_cost,
    resolve_agent_prompt,
)

# Backward-compatible aliases for tests and external code
_TRANSFER_CALL_TOOL = TRANSFER_CALL_TOOL
_END_CALL_TOOL = END_CALL_TOOL

logger = logging.getLogger("getpatter")

# Maximum size (bytes) of a single WebSocket message accepted from Twilio.
# Twilio audio frames are ~160 bytes (mulaw 8 kHz, 20 ms).  1 MB is
# extremely generous and defends against memory exhaustion from a malformed
# or malicious stream peer.
_MAX_WS_MESSAGE_BYTES = 1 * 1024 * 1024


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
    return bool(re.match(r"^[A-Z]{2}[0-9a-f]{32}$", sid))


def _xml_escape(s: str) -> str:
    """Escape special XML characters."""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


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
    from getpatter.providers.twilio_adapter import TwilioAdapter  # type: ignore[import]

    stream_url = (
        f"wss://{webhook_base_url}/ws/stream/{call_sid}"
        f"?caller={quote(caller)}&callee={quote(callee)}"
    )
    return TwilioAdapter.generate_stream_twiml(stream_url)


# ---------------------------------------------------------------------------
# Twilio AudioSender — transcodes PCM 16 kHz to mulaw 8 kHz
# ---------------------------------------------------------------------------


class TwilioAudioSender(AudioSender):
    """Sends audio to a Twilio WebSocket, transcoding PCM to mulaw.

    When ``input_is_mulaw_8k`` is True, incoming bytes are already in Twilio's
    native codec (g711 mulaw @ 8 kHz) and are forwarded as-is. This is the
    correct path for OpenAI Realtime on Twilio — feeding OpenAI's 24 kHz PCM16
    into a 16 → 8 kHz resampler produces audibly broken audio.
    """

    def __init__(
        self, websocket, stream_sid: str, input_is_mulaw_8k: bool = False
    ) -> None:
        self._ws = websocket
        self._stream_sid = stream_sid
        self._chunk_count = 0
        self.last_confirmed_mark = ""
        self._input_is_mulaw_8k = input_is_mulaw_8k
        # Lazy import transcoding helpers (only needed when transcoding).
        # ``PcmCarry`` mirrors TS ``StreamHandler.alignPcm16``: HTTP TTS
        # providers can yield odd-length chunks that would otherwise crash
        # ``audioop.ratecv`` with "not a whole number of frames".
        if not input_is_mulaw_8k:
            from getpatter.audio.transcoding import (
                PcmCarry,
                create_resampler_16k_to_8k,
                pcm16_to_mulaw,
            )

            self._pcm16_to_mulaw = pcm16_to_mulaw
            # StatefulResampler preserves audioop.ratecv IIR filter state
            # across chunks (the old stateless path discarded the state token
            # on every call, which caused aliasing artefacts even with
            # PcmCarry alignment). PcmCarry is kept for odd-byte alignment
            # because StatefulResampler.process() still expects even-length
            # PCM16 input.
            self._resampler = create_resampler_16k_to_8k()
            self._pcm_carry: PcmCarry | None = PcmCarry()
        else:
            self._pcm16_to_mulaw = None
            self._resampler = None
            self._pcm_carry = None

    def reset_pcm_carry(self) -> None:
        """Drop any buffered odd byte. Call at the start of a new TTS synthesis."""
        if self._pcm_carry is not None:
            self._pcm_carry.reset()

    async def send_audio(self, pcm_audio: bytes) -> None:
        """Send a chunk of audio to Twilio, transcoding to mulaw 8 kHz when needed."""
        if self._input_is_mulaw_8k:
            mulaw = pcm_audio
        else:
            aligned = self._pcm_carry.align(pcm_audio)  # type: ignore[union-attr]
            if not aligned:
                return
            resampled = self._resampler.process(aligned)  # type: ignore[union-attr]
            mulaw = self._pcm16_to_mulaw(resampled)
        encoded = base64.b64encode(mulaw).decode("ascii")
        await self._ws.send_text(
            json.dumps(
                {
                    "event": "media",
                    "streamSid": self._stream_sid,
                    "media": {"payload": encoded},
                }
            )
        )

    async def send_clear(self) -> None:
        """Tell Twilio to flush any buffered playback (used on barge-in)."""
        await self._ws.send_text(
            json.dumps({"event": "clear", "streamSid": self._stream_sid})
        )

    async def send_mark(self, mark_name: str) -> None:
        """Send a Twilio media-stream mark frame to track playback completion."""
        self._chunk_count += 1
        actual_name = f"audio_{self._chunk_count}"
        await self._ws.send_text(
            json.dumps(
                {
                    "event": "mark",
                    "streamSid": self._stream_sid,
                    "mark": {"name": actual_name},
                }
            )
        )

    def on_mark_confirmed(self, mark_name: str) -> None:
        """Record that Twilio has finished playing back the named mark."""
        self.last_confirmed_mark = mark_name

    async def flush(self) -> None:
        """Send any resampler tail bytes before closing the stream.

        Drains the StatefulResampler carry buffer and sends the remaining
        even-aligned PCM16 → mulaw bytes to Twilio. Call this on the stop /
        hangup path to avoid clipping the last audio frame. The PcmCarry
        buffer is intentionally not drained here — any final odd byte is
        sub-sample noise that would produce a single corrupted sample.
        No-op when input_is_mulaw_8k=True.
        """
        if self._resampler is None or self._pcm16_to_mulaw is None:
            return
        tail = self._resampler.flush()
        if tail:
            mulaw = self._pcm16_to_mulaw(tail)
            encoded = base64.b64encode(mulaw).decode("ascii")
            await self._ws.send_text(
                json.dumps(
                    {
                        "event": "media",
                        "streamSid": self._stream_sid,
                        "media": {"payload": encoded},
                    }
                )
            )


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
    on_metrics=None,
    pricing: dict | None = None,
    report_only_initial_ttfb: bool = False,
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
    conversation_history: deque[dict] = deque(maxlen=200)
    transcript_entries: deque[dict] = deque(maxlen=200)

    handler: (
        OpenAIRealtimeStreamHandler
        | ElevenLabsConvAIStreamHandler
        | PipelineStreamHandler
        | None
    ) = None
    audio_sender: TwilioAudioSender | None = None
    metrics = None

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

                # Single INFO line per call-start — full context in one place.
                _mode = (
                    f"engine={getattr(agent, 'provider', 'unknown')}"
                    if getattr(agent, "engine", None) is None
                    else f"engine={getattr(agent.engine, 'kind', 'unknown')}"
                )
                if (
                    getattr(agent, "stt", None) is not None
                    and getattr(agent, "tts", None) is not None
                    and getattr(agent, "engine", None) is None
                ):
                    _mode = "pipeline"
                logger.info(
                    "Call started: %s (Twilio, %s, %s → %s)",
                    call_sid_actual,
                    _mode,
                    caller or "?",
                    callee or "?",
                )
                if custom_params:
                    logger.debug("Custom params: %s", custom_params)

                # Fire on_call_start callback — may return per-call config overrides
                _call_overrides = None
                if on_call_start:
                    _call_overrides = await on_call_start(
                        {
                            "call_id": call_sid_actual,
                            "caller": caller,
                            "callee": callee,
                            "direction": "inbound",
                            "custom_params": custom_params,
                            "telephony_provider": "twilio",
                        }
                    )
                    if not isinstance(_call_overrides, dict):
                        _call_overrides = None

                # Apply per-call overrides (dynamic agent config)
                if _call_overrides:
                    agent = apply_call_overrides(agent, _call_overrides)

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
                            logger.debug("Recording started for %s", call_sid_actual)
                        except Exception as _exc:
                            logger.warning("Could not start recording: %s", _exc)

                resolved_prompt = resolve_agent_prompt(agent, custom_params)
                provider = getattr(agent, "provider", "openai_realtime")

                # Initialize metrics
                metrics = create_metrics_accumulator(
                    call_id=call_sid_actual,
                    provider=provider,
                    telephony_provider="twilio",
                    agent=agent,
                    deepgram_key=deepgram_key,
                    elevenlabs_key=elevenlabs_key,
                    pricing=pricing,
                    report_only_initial_ttfb=report_only_initial_ttfb,
                )
                # Twilio uses mulaw 8kHz (1 byte/sample)
                metrics.configure_stt_format(sample_rate=8000, bytes_per_sample=1)

                # Create audio sender. OpenAI Realtime on Twilio is configured
                # to emit g711_ulaw @ 8 kHz directly (see below), so for that
                # provider we skip the built-in PCM→mulaw transcoding path.
                # Pipeline / ConvAI still produce PCM16 @ 16 kHz.
                _input_is_mulaw = provider == "openai_realtime"
                audio_sender = TwilioAudioSender(
                    websocket, stream_sid, input_is_mulaw_8k=_input_is_mulaw
                )

                # --- Twilio-specific call control helpers ---
                async def _twilio_transfer(number):
                    if not _validate_e164(number):
                        logger.warning(
                            "transfer rejected: invalid number %s",
                            mask_phone_number(number),
                        )
                        return
                    if twilio_sid and twilio_token and call_sid_actual:
                        if not _validate_twilio_sid(call_sid_actual, "CA"):
                            logger.warning(
                                "transfer skipped: invalid CallSid %r", call_sid_actual
                            )
                            return
                        import httpx as _httpx

                        async with _httpx.AsyncClient() as _http:
                            twiml = f"<Response><Dial>{_xml_escape(number)}</Dial></Response>"
                            await _http.post(
                                f"https://api.twilio.com/2010-04-01/Accounts/{twilio_sid}/Calls/{call_sid_actual}.json",
                                auth=(twilio_sid, twilio_token),
                                data={"Twiml": twiml},
                            )
                        logger.debug(
                            "Call transferred to %s", mask_phone_number(number)
                        )

                async def _twilio_hangup():
                    if twilio_sid and twilio_token and call_sid_actual:
                        if not _validate_twilio_sid(call_sid_actual, "CA"):
                            logger.warning(
                                "hangup skipped: invalid CallSid %r", call_sid_actual
                            )
                            return
                        import httpx as _httpx

                        async with _httpx.AsyncClient() as _http:
                            await _http.post(
                                f"https://api.twilio.com/2010-04-01/Accounts/{twilio_sid}/Calls/{call_sid_actual}.json",
                                auth=(twilio_sid, twilio_token),
                                data={"Status": "completed"},
                            )
                        logger.debug("Call hung up")

                # Create the appropriate stream handler
                if provider == "pipeline":
                    handler = PipelineStreamHandler(
                        agent=agent,
                        audio_sender=audio_sender,
                        call_id=call_sid_actual,
                        caller=caller,
                        callee=callee,
                        resolved_prompt=resolved_prompt,
                        metrics=metrics,
                        openai_key=openai_key,
                        deepgram_key=deepgram_key,
                        elevenlabs_key=elevenlabs_key,
                        for_twilio=True,
                        transfer_fn=_twilio_transfer,
                        hangup_fn=_twilio_hangup,
                        on_transcript=on_transcript,
                        on_message=on_message,
                        on_metrics=on_metrics,
                        conversation_history=conversation_history,
                        transcript_entries=transcript_entries,
                    )
                elif provider == "elevenlabs_convai":
                    handler = ElevenLabsConvAIStreamHandler(
                        agent=agent,
                        audio_sender=audio_sender,
                        call_id=call_sid_actual,
                        caller=caller,
                        callee=callee,
                        resolved_prompt=resolved_prompt,
                        metrics=metrics,
                        elevenlabs_key=elevenlabs_key,
                        for_twilio=True,
                        on_transcript=on_transcript,
                        on_metrics=on_metrics,
                        conversation_history=conversation_history,
                        transcript_entries=transcript_entries,
                    )
                else:
                    handler = OpenAIRealtimeStreamHandler(
                        agent=agent,
                        audio_sender=audio_sender,
                        call_id=call_sid_actual,
                        caller=caller,
                        callee=callee,
                        resolved_prompt=resolved_prompt,
                        metrics=metrics,
                        openai_key=openai_key,
                        transfer_fn=_twilio_transfer,
                        hangup_fn=_twilio_hangup,
                        on_transcript=on_transcript,
                        on_metrics=on_metrics,
                        conversation_history=conversation_history,
                        transcript_entries=transcript_entries,
                        # Twilio media streams are g711 mulaw @ 8 kHz. Asking
                        # OpenAI to emit the same codec avoids a 24 kHz →
                        # 16 kHz → 8 kHz resample chain that otherwise
                        # produces a deep, slurred voice.
                        audio_format="g711_ulaw",
                    )

                await handler.start()

            elif event == "media":
                payload = data.get("media", {}).get("payload", "")
                mulaw_audio = base64.b64decode(payload)
                if handler is not None:
                    await handler.on_audio_received(mulaw_audio)

            elif event == "mark":
                mark_name = data.get("mark", {}).get("name", "")
                if isinstance(
                    getattr(handler, "audio_sender", None), TwilioAudioSender
                ):
                    handler.audio_sender.on_mark_confirmed(mark_name)
                if handler is not None:
                    await handler.on_mark(mark_name)

            elif event == "dtmf":
                digit = data.get("dtmf", {}).get("digit", "")
                logger.debug("DTMF: %s", digit)
                if handler is not None:
                    await handler.on_dtmf(digit)
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
        # Flush resampler tail before tearing down — drains any carry bytes so
        # the last audio frame isn't clipped on graceful shutdown.
        if audio_sender is not None:
            try:
                await audio_sender.flush()
            except Exception as _exc:
                logger.debug("Twilio audio_sender flush failed: %s", _exc)

        if handler is not None:
            await handler.cleanup()

        # --- Metrics: query actual telephony cost from Twilio ---
        if (
            metrics is not None
            and twilio_sid
            and twilio_token
            and call_sid_actual
            and _validate_twilio_sid(call_sid_actual, "CA")
        ):
            try:
                import httpx as _httpx

                async with _httpx.AsyncClient() as _http:
                    resp = await _http.get(
                        f"https://api.twilio.com/2010-04-01/Accounts/{twilio_sid}/Calls/{call_sid_actual}.json",
                        auth=(twilio_sid, twilio_token),
                        timeout=5.0,
                    )
                    if resp.status_code == 200:
                        call_data = resp.json()
                        price = call_data.get("price")
                        if price is not None:
                            # Twilio returns price as negative string (e.g. "-0.0085")
                            metrics.set_actual_telephony_cost(abs(float(price)))
                            logger.debug("Twilio actual cost: $%s", abs(float(price)))
            except Exception as exc:
                logger.debug("Could not fetch Twilio call cost: %s", exc)

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
                        "call_id": call_sid_actual,
                        "caller": caller,
                        "callee": callee,
                        "ended_at": time.time(),
                        "transcript": list(conversation_history),
                        "metrics": call_metrics,
                    }
                )
            except Exception as exc:
                logger.exception("on_call_end error: %s", exc)

        # Single INFO line per call-end — duration, turns, cost, latency.
        if call_metrics is not None:
            _dur = getattr(call_metrics, "duration_seconds", 0) or 0
            _turns = len(getattr(call_metrics, "turns", []) or [])
            _cost = getattr(getattr(call_metrics, "cost", None), "total", 0) or 0
            _p95 = (
                getattr(getattr(call_metrics, "latency_p95", None), "total_ms", 0) or 0
            )
            logger.info(
                "Call ended: %s (%.1fs, %d turns, cost=$%.4f, p95=%dms)",
                call_sid_actual,
                _dur,
                _turns,
                _cost,
                round(_p95),
            )
        else:
            logger.info("Call ended: %s", call_sid_actual)
