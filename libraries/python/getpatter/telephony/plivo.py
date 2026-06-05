"""Plivo webhook and stream handlers for local mode.

Plivo is a hybrid of the two existing carriers: it answers synchronously with
XML like Twilio (the WSS URL is the ``<Stream>`` element's *text content*),
streams **mulaw 8 kHz** like Twilio (we pin it via the answer XML's
``contentType``), and exposes a ``checkpoint`` → ``playedStream`` acknowledgement
flow analogous to Twilio's media-stream marks. Unlike Twilio it also supports
**native DTMF send** over the WebSocket (``sendDTMF``).

Wire protocol reference: ``agent-transport``'s
``crates/agent-transport/src/audio_stream/plivo.rs``.
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import time
from collections import deque
from urllib.parse import quote

import httpx

from getpatter.observability.attributes import patter_call_scope
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
from getpatter.telephony.common import (
    _create_stt_from_config,  # noqa: F401 — re-exported for tests and external callers
    _create_tts_from_config,  # noqa: F401 — re-exported for tests and external callers
    _resolve_variables,  # noqa: F401 — re-exported for tests and external callers
    _sanitize_variable_value,  # noqa: F401 — re-exported for tests and external callers
    _validate_e164,
)
from getpatter.utils.log_sanitize import mask_phone_number

# Backward-compatible aliases for tests and external code
_TRANSFER_CALL_TOOL = TRANSFER_CALL_TOOL
_END_CALL_TOOL = END_CALL_TOOL

logger = logging.getLogger("getpatter")

# Maximum size (bytes) of a single WebSocket message accepted from Plivo.
# Plivo audio frames are ~160 bytes (mulaw 8 kHz, 20 ms). 1 MB is extremely
# generous and defends against memory exhaustion from a malformed or malicious
# stream peer. Mirrors the Twilio bridge.
_MAX_WS_MESSAGE_BYTES = 1 * 1024 * 1024

PLIVO_API_BASE = "https://api.plivo.com/v1"

# DTMF digits Plivo accepts over the ``sendDTMF`` command. Parity with the
# Telnyx allowlist and TS ``PLIVO_DTMF_ALLOWED``.
_DTMF_ALLOWED = frozenset("0123456789*#ABCDabcdwW")


def _parse_plivo_extra_headers(raw: str) -> dict[str, str]:
    """Parse Plivo's ``extra_headers`` start-frame field into a dict.

    Plivo delivers the ``<Stream extraHeaders="...">`` payload back on the
    ``start`` frame in one of several shapes. Port of
    ``parse_extra_headers`` in ``agent-transport``'s ``plivo.rs``:

    * JSON object — ``{"key": "value"}``
    * Plivo brace form — ``{key: value, key2: value2}``
    * Delimited pairs — ``k=v;k2=v2`` or ``k=v,k2=v2``
    """
    raw = (raw or "").strip()
    if not raw:
        return {}
    headers: dict[str, str] = {}
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return {str(k): str(v) for k, v in parsed.items()}
        except (ValueError, TypeError):
            pass
        inner = raw.lstrip("{").rstrip("}")
        for part in inner.split(","):
            if ":" in part:
                k, _, v = part.partition(":")
                headers[k.strip()] = v.strip()
        return headers
    for part in raw.replace(";", ",").split(","):
        if "=" in part:
            k, _, v = part.partition("=")
            headers[k.strip()] = v.strip()
    return headers


async def handle_amd_result(
    call_uuid: str,
    voicemail_message: str,
    auth_id: str,
    auth_token: str,
) -> None:
    """Speak a voicemail message on a machine-answered call, then hang up.

    Mirrors :func:`getpatter.telephony.telnyx.handle_amd_result`. Uses Plivo's
    live-call Speak API (``POST /Call/{uuid}/Speak/``), waits an estimated
    playout window (~60 ms/char, capped at 30 s) so the message isn't cut off,
    then hangs up via ``DELETE /Call/{uuid}/``. Best-effort — errors are logged
    and never raised back into the webhook handler.
    """
    if not (call_uuid and voicemail_message and auth_id and auth_token):
        return
    import asyncio as _asyncio

    base = f"{PLIVO_API_BASE}/Account/{auth_id}/Call/{quote(call_uuid, safe='')}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            # Plivo's Speak API expects form-encoded body per the docs
            # (https://www.plivo.com/docs/voice/api/call/speak-text-on-calls).
            speak = await http.post(
                f"{base}/Speak/",
                auth=(auth_id, auth_token),
                data={"text": voicemail_message},
            )
            if speak.status_code >= 400:
                logger.warning(
                    "Plivo voicemail Speak failed (%d): %s",
                    speak.status_code,
                    speak.text[:200],
                )
                return
            await _asyncio.sleep(min(30.0, len(voicemail_message) * 0.06))
            await http.delete(f"{base}/", auth=(auth_id, auth_token))
        logger.info("Voicemail dropped for %s", call_uuid)
    except Exception as exc:  # noqa: BLE001 — best-effort voicemail drop
        logger.warning("Could not drop voicemail: %s", exc)


def plivo_webhook_handler(
    call_id: str,
    caller: str,
    callee: str,
    webhook_base_url: str,
) -> str:
    """Generate Plivo answer XML for an incoming (or answered outbound) call.

    Returns an XML string telling Plivo to open a bidirectional mulaw 8 kHz
    media stream to our WebSocket. Caller / callee travel on the WSS query
    string (Plivo preserves it) with an ``extraHeaders`` fallback delivered on
    the ``start`` frame.

    Args:
        call_id: Plivo CallUUID (or a placeholder for outbound answer_url).
        caller: The calling number (From).
        callee: The called number (To).
        webhook_base_url: Hostname (no scheme), e.g. ``"abc.ngrok.io"``.
    """
    # Lazy import — provider adapter may be created by the parallel agent.
    from getpatter.providers.plivo_adapter import PlivoAdapter  # type: ignore[import]

    qs = f"?caller={quote(caller)}&callee={quote(callee)}"
    stream_url = f"wss://{webhook_base_url}/ws/plivo/stream/{call_id}{qs}"
    # Fallback channel: Plivo echoes these on the ``start`` frame's
    # ``extra_headers`` when the query string is unavailable.
    extra_headers = {"X-PH-caller": caller, "X-PH-callee": callee}
    return PlivoAdapter.generate_stream_xml(
        stream_url,
        content_type="audio/x-mulaw;rate=8000",
        extra_headers=extra_headers,
    )


# ---------------------------------------------------------------------------
# Plivo AudioSender — transcodes PCM 16 kHz to mulaw 8 kHz
# ---------------------------------------------------------------------------


class PlivoAudioSender(AudioSender):
    """Sends audio to a Plivo WebSocket, transcoding PCM to mulaw.

    Mirrors :class:`~getpatter.telephony.twilio.TwilioAudioSender` — the
    transcode pipeline is identical (mulaw 8 kHz). Only the JSON envelopes
    differ: outbound audio is a ``playAudio`` command (carrying its own
    ``contentType`` / ``sampleRate``), barge-in flush is ``clearAudio``, and
    the playback marker is ``checkpoint`` (acknowledged by ``playedStream``).
    Plivo also accepts native ``sendDTMF``.

    When ``input_is_mulaw_8k`` is True, incoming bytes are already in Plivo's
    native codec (g711 mulaw @ 8 kHz) and are forwarded as-is — the correct
    path for OpenAI Realtime (``audio_format="g711_ulaw"``).
    """

    def __init__(
        self,
        websocket,
        stream_id: str,
        input_is_mulaw_8k: bool = False,
        content_type: str = "audio/x-mulaw",
        sample_rate: int = 8000,
    ) -> None:
        self._ws = websocket
        self._stream_id = stream_id
        self._content_type = content_type
        self._sample_rate = sample_rate
        self._chunk_count = 0
        self.last_confirmed_mark = ""
        self._input_is_mulaw_8k = input_is_mulaw_8k
        if not input_is_mulaw_8k:
            from getpatter.audio.transcoding import (
                PcmCarry,
                create_resampler_16k_to_8k,
                pcm16_to_mulaw,
            )

            self._pcm16_to_mulaw = pcm16_to_mulaw
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

    async def _send_play_audio(self, mulaw: bytes) -> None:
        encoded = base64.b64encode(mulaw).decode("ascii")
        await self._ws.send_text(
            json.dumps(
                {
                    "event": "playAudio",
                    "media": {
                        "contentType": self._content_type,
                        "sampleRate": self._sample_rate,
                        "payload": encoded,
                    },
                }
            )
        )

    async def send_audio(self, pcm_audio: bytes) -> None:
        """Send a chunk of audio to Plivo, transcoding to mulaw 8 kHz when needed."""
        if self._input_is_mulaw_8k:
            mulaw = pcm_audio
        else:
            aligned = self._pcm_carry.align(pcm_audio)  # type: ignore[union-attr]
            if not aligned:
                return
            resampled = self._resampler.process(aligned)  # type: ignore[union-attr]
            mulaw = self._pcm16_to_mulaw(resampled)
        await self._send_play_audio(mulaw)

    async def send_clear(self) -> None:
        """Tell Plivo to flush any buffered playback (used on barge-in)."""
        await self._ws.send_text(
            json.dumps({"event": "clearAudio", "streamId": self._stream_id})
        )

    async def send_mark(self, mark_name: str) -> None:
        """Send a Plivo ``checkpoint`` to track playback completion.

        Plivo acknowledges with a ``playedStream`` event carrying the same
        name once all audio queued before the checkpoint has played out —
        the analogue of Twilio's mark protocol that gates pacing / barge-in.
        """
        self._chunk_count += 1
        actual_name = f"audio_{self._chunk_count}"
        await self._ws.send_text(
            json.dumps(
                {
                    "event": "checkpoint",
                    "streamId": self._stream_id,
                    "name": actual_name,
                }
            )
        )

    def on_mark_confirmed(self, mark_name: str) -> None:
        """Record that Plivo has finished playing back the named checkpoint."""
        self.last_confirmed_mark = mark_name

    async def send_dtmf(self, digits: str) -> None:
        """Send DTMF digits to the caller over the stream (Plivo ``sendDTMF``).

        A capability Twilio Media Streams lacks. Invalid digits are dropped.
        """
        filtered = "".join(d for d in (digits or "") if d in _DTMF_ALLOWED)
        if not filtered:
            logger.warning("Plivo send_dtmf: no valid digits in %r", digits)
            return
        await self._ws.send_text(json.dumps({"event": "sendDTMF", "dtmf": filtered}))

    async def flush(self) -> None:
        """Send any resampler tail bytes before closing the stream.

        Drains the StatefulResampler carry buffer so the last audio frame
        isn't clipped on graceful shutdown. No-op when input_is_mulaw_8k=True.
        """
        if self._resampler is None or self._pcm16_to_mulaw is None:
            return
        tail = self._resampler.flush()
        if tail:
            mulaw = self._pcm16_to_mulaw(tail)
            await self._send_play_audio(mulaw)


async def plivo_stream_bridge(
    websocket,
    agent,
    openai_key: str,
    on_call_start=None,
    on_call_end=None,
    on_transcript=None,
    on_message=None,
    deepgram_key: str = "",
    elevenlabs_key: str = "",
    plivo_auth_id: str = "",
    plivo_auth_token: str = "",
    webhook_host: str = "",
    recording: bool = False,
    on_metrics=None,
    on_transcript_line=None,
    pricing: dict | None = None,
    report_only_initial_ttfb: bool = False,
    speech_events=None,
    patter_side: str = "uut",
    pop_prewarm_audio=None,
    pop_prewarmed_connections=None,
) -> None:
    """Bridge a Plivo WebSocket media stream to the configured AI provider.

    Mirrors :func:`~getpatter.telephony.twilio.twilio_stream_bridge`. Supports
    ``openai_realtime`` (default), ``elevenlabs_convai`` and ``pipeline``
    provider modes. ``plivo_auth_id`` / ``plivo_auth_token`` authenticate REST
    call control (transfer / hangup / recording / cost) and ``webhook_host``
    is the public hostname used to build the blind-transfer ``aleg_url``.
    """
    await websocket.accept()

    caller: str = websocket.query_params.get("caller", "")
    callee: str = websocket.query_params.get("callee", "")

    stream_id: str | None = None
    call_id_actual: str = ""
    conversation_history: deque[dict] = deque(maxlen=200)
    transcript_entries: deque[dict] = deque(maxlen=200)

    handler: (
        OpenAIRealtimeStreamHandler
        | ElevenLabsConvAIStreamHandler
        | PipelineStreamHandler
        | None
    ) = None
    audio_sender: PlivoAudioSender | None = None
    metrics = None

    _call_start_monotonic: float | None = None
    _scope_stack = contextlib.ExitStack()

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
                _call_start_monotonic = time.monotonic()
                start_data = data.get("start", {})
                # Plivo's CallUUID arrives here as ``callId`` and is the id
                # used for hangup / transfer / recording / cost REST calls.
                call_id_actual = start_data.get("callId", "")
                stream_id = start_data.get("streamId", "")
                media_format = start_data.get("mediaFormat", {}) or {}

                # Recover caller / callee: query string first (Plivo preserves
                # it on the Stream URL), then the ``extra_headers`` fallback.
                if not caller or not callee:
                    hdrs = _parse_plivo_extra_headers(data.get("extra_headers", ""))
                    caller = caller or hdrs.get("X-PH-caller", "")
                    callee = callee or hdrs.get("X-PH-callee", "")

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
                    "Call started: %s (Plivo, %s, %s → %s)",
                    call_id_actual,
                    _mode,
                    caller or "?",
                    callee or "?",
                )
                if media_format:
                    logger.debug("Plivo mediaFormat: %s", media_format)

                # Fire on_call_start callback — may return per-call config overrides
                _call_overrides = None
                if on_call_start:
                    _call_overrides = await on_call_start(
                        {
                            "call_id": call_id_actual,
                            "caller": caller,
                            "callee": callee,
                            "direction": "inbound",
                            "custom_params": {},
                            "telephony_provider": "plivo",
                        }
                    )
                    if not isinstance(_call_overrides, dict):
                        _call_overrides = None
                if _call_overrides:
                    agent = apply_call_overrides(agent, _call_overrides)

                # Start recording if requested (Plivo Record API).
                if recording and plivo_auth_id and plivo_auth_token and call_id_actual:
                    try:
                        async with httpx.AsyncClient() as _http:
                            await _http.post(
                                f"{PLIVO_API_BASE}/Account/{plivo_auth_id}/Call/{quote(call_id_actual, safe='')}/Record/",
                                auth=(plivo_auth_id, plivo_auth_token),
                                timeout=10.0,
                            )
                        logger.debug("Recording started for %s", call_id_actual)
                    except Exception as _exc:
                        logger.warning("Could not start recording: %s", _exc)

                resolved_prompt = resolve_agent_prompt(agent, {})
                provider = getattr(agent, "provider", "openai_realtime")

                metrics = create_metrics_accumulator(
                    call_id=call_id_actual,
                    provider=provider,
                    telephony_provider="plivo",
                    agent=agent,
                    deepgram_key=deepgram_key,
                    elevenlabs_key=elevenlabs_key,
                    pricing=pricing,
                    report_only_initial_ttfb=report_only_initial_ttfb,
                )
                metrics.configure_stt_format(sample_rate=16000, bytes_per_sample=2)

                # OpenAI Realtime emits g711_ulaw @ 8 kHz directly (see below),
                # so for that provider we skip the PCM→mulaw transcode path.
                # Pipeline / ConvAI produce PCM16 @ 16 kHz.
                _input_is_mulaw = provider in ("openai_realtime", "openai_realtime_2")
                audio_sender = PlivoAudioSender(
                    websocket, stream_id or "", input_is_mulaw_8k=_input_is_mulaw
                )

                # --- Plivo-specific call control helpers ---
                async def _plivo_transfer(number):
                    if not _validate_e164(number):
                        logger.warning(
                            "transfer rejected: invalid number %s",
                            mask_phone_number(number),
                        )
                        return
                    if not (plivo_auth_id and plivo_auth_token and call_id_actual):
                        return
                    if not webhook_host:
                        logger.warning(
                            "transfer skipped: no webhook_host for Plivo aleg_url"
                        )
                        return
                    # Plivo blind transfer redirects the A-leg to new XML.
                    aleg_url = (
                        f"https://{webhook_host}/webhooks/plivo/transfer"
                        f"?to={quote(number)}"
                    )
                    async with httpx.AsyncClient() as _http:
                        await _http.post(
                            f"{PLIVO_API_BASE}/Account/{plivo_auth_id}/Call/{quote(call_id_actual, safe='')}/",
                            auth=(plivo_auth_id, plivo_auth_token),
                            json={
                                "legs": "aleg",
                                "aleg_url": aleg_url,
                                "aleg_method": "GET",
                            },
                            timeout=10.0,
                        )
                    logger.debug("Call transferred to %s", mask_phone_number(number))

                async def _plivo_hangup(*_args):
                    if not (plivo_auth_id and plivo_auth_token and call_id_actual):
                        return
                    async with httpx.AsyncClient() as _http:
                        resp = await _http.delete(
                            f"{PLIVO_API_BASE}/Account/{plivo_auth_id}/Call/{quote(call_id_actual, safe='')}/",
                            auth=(plivo_auth_id, plivo_auth_token),
                            timeout=10.0,
                        )
                    if resp.status_code not in (204, 404):
                        logger.warning(
                            "Plivo hangup returned %s: %s",
                            resp.status_code,
                            resp.text[:200],
                        )
                    else:
                        logger.debug("Call hung up")

                async def _plivo_send_dtmf(digits: str, delay_ms: int = 0) -> None:
                    # Plivo sends DTMF over the media WebSocket (not REST).
                    if audio_sender is not None:
                        await audio_sender.send_dtmf(digits)

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
                        # Plivo streams mulaw 8 kHz (pinned via the answer XML
                        # contentType), identical to Twilio — so reuse the
                        # Twilio mulaw STT/transcode config.
                        for_twilio=True,
                        transfer_fn=_plivo_transfer,
                        hangup_fn=_plivo_hangup,
                        send_dtmf_fn=_plivo_send_dtmf,
                        on_transcript=on_transcript,
                        on_message=on_message,
                        on_metrics=on_metrics,
                        conversation_history=conversation_history,
                        transcript_entries=transcript_entries,
                        pop_prewarm_audio=pop_prewarm_audio,
                        pop_prewarmed_connections=pop_prewarmed_connections,
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
                        for_twilio=True,
                        on_transcript=on_transcript,
                        on_metrics=on_metrics,
                        on_transcript_line=on_transcript_line,
                        conversation_history=conversation_history,
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
                        transfer_fn=_plivo_transfer,
                        hangup_fn=_plivo_hangup,
                        on_transcript=on_transcript,
                        on_metrics=on_metrics,
                        on_transcript_line=on_transcript_line,
                        conversation_history=conversation_history,
                        transcript_entries=transcript_entries,
                        # Plivo media streams are g711 mulaw @ 8 kHz. Asking
                        # OpenAI to emit the same codec avoids a 24 kHz →
                        # 16 kHz → 8 kHz resample chain that otherwise
                        # produces a deep, slurred voice.
                        audio_format="g711_ulaw",
                        speech_events=speech_events,
                        pop_prewarmed_connections=pop_prewarmed_connections,
                    )

                try:
                    handler._patter_side = patter_side
                except Exception:  # pragma: no cover — defense in depth
                    logger.debug("Failed to set handler._patter_side", exc_info=True)

                try:
                    if call_id_actual:
                        _scope_stack.enter_context(
                            patter_call_scope(call_id=call_id_actual, side=patter_side)
                        )
                except Exception:  # pragma: no cover — defense in depth
                    logger.debug("patter_call_scope entry failed", exc_info=True)

                await handler.start()

            elif event == "media":
                payload = data.get("media", {}).get("payload", "")
                mulaw_audio = base64.b64decode(payload)
                if handler is not None:
                    await handler.on_audio_received(mulaw_audio)

            elif event == "playedStream":
                # Checkpoint acknowledgement — the analogue of a Twilio mark.
                mark_name = data.get("name", "")
                if isinstance(getattr(handler, "audio_sender", None), PlivoAudioSender):
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
                            "call_id": call_id_actual,
                        }
                    )

            elif event in ("playFailed", "error"):
                logger.warning("Plivo %s: %s", event, data.get("reason", "unknown"))

            elif event == "clearedAudio":
                logger.debug("Plivo confirmed audio buffer cleared")

            elif event == "stop":
                break

    except Exception as exc:
        logger.exception("Stream error: %s", exc)
    finally:
        if audio_sender is not None:
            try:
                await audio_sender.flush()
            except Exception as _exc:
                logger.debug("Plivo audio_sender flush failed: %s", _exc)

        if handler is not None:
            await handler.cleanup()

        # --- Observability: emit patter.cost.telephony_minutes ---
        if _call_start_monotonic is not None and plivo_auth_id and plivo_auth_token:
            try:
                from getpatter.providers.plivo_adapter import PlivoAdapter

                _duration = time.monotonic() - _call_start_monotonic
                PlivoAdapter(
                    auth_id=plivo_auth_id, auth_token=plivo_auth_token
                ).record_call_end_cost(duration_seconds=_duration, direction="inbound")
            except Exception as exc:
                logger.debug("record_call_end_cost failed: %s", exc)

        # --- Metrics: query actual telephony cost from Plivo CDR ---
        if (
            metrics is not None
            and plivo_auth_id
            and plivo_auth_token
            and call_id_actual
        ):
            try:
                async with httpx.AsyncClient() as _http:
                    resp = await _http.get(
                        f"{PLIVO_API_BASE}/Account/{plivo_auth_id}/Call/{quote(call_id_actual, safe='')}/",
                        auth=(plivo_auth_id, plivo_auth_token),
                        timeout=5.0,
                    )
                    if resp.status_code == 200:
                        amount = resp.json().get("total_amount")
                        if amount is not None:
                            metrics.set_actual_telephony_cost(abs(float(amount)))
                            logger.debug("Plivo actual cost: $%s", amount)
            except Exception as exc:
                logger.debug("Could not fetch Plivo call cost: %s", exc)

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
                        "transcript": list(conversation_history),
                        "metrics": call_metrics,
                    }
                )
            except Exception as exc:
                logger.exception("on_call_end error: %s", exc)

        if call_metrics is not None:
            _dur = getattr(call_metrics, "duration_seconds", 0) or 0
            _turns = len(getattr(call_metrics, "turns", []) or [])
            _cost = getattr(getattr(call_metrics, "cost", None), "total", 0) or 0
            _p95_obj = getattr(call_metrics, "latency_p95", None)
            _p95 = (
                getattr(_p95_obj, "agent_response_ms", None)
                or getattr(_p95_obj, "total_ms", 0)
                or 0
            )
            logger.info(
                "Call ended: %s (%.1fs, %d turns, cost=$%.4f, p95 wait=%dms)",
                call_id_actual,
                _dur,
                _turns,
                _cost,
                round(_p95),
            )
        else:
            logger.info("Call ended: %s", call_id_actual)

        try:
            _scope_stack.close()
        except Exception:  # pragma: no cover — defense in depth
            logger.debug("ExitStack close failed", exc_info=True)
