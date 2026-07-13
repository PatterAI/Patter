"""xAI (Grok) Voice Agent realtime adapter — all-in-one STT + LLM + TTS over
WebSocket.

Targets ``wss://api.x.ai/v1/realtime`` (the xAI Voice Agent API,
https://docs.x.ai/developers/model-capabilities/audio/voice-agent). The wire
protocol is OpenAI-Realtime-shaped — ``session.update``,
``input_audio_buffer.append``, ``conversation.item.create``,
``response.create`` — with xAI-specific extensions:

* GA-style nested audio config: ``session.audio.{input,output}.format``
  with MIME types (``audio/pcm`` / ``audio/pcmu`` / ``audio/pcma``) and a
  configurable PCM sample rate (8 kHz–48 kHz, default 24 kHz).
* Server-side tools (``web_search``, ``x_search``, ``file_search``,
  ``mcp``) alongside classic ``function`` tools.
* ``audio.input.transcription.language_hint`` / ``.keyterms`` ASR biasing,
  updatable mid-session.
* ``replace`` — pronunciation replacement map applied to the spoken audio
  only (the transcript keeps the original text).
* ``reasoning.effort`` (``"high"`` / ``"none"``) on the thinking voice
  models.
* GA event names: ``response.output_audio.delta`` /
  ``response.output_audio_transcript.delta`` (this adapter also accepts the
  legacy ``response.audio.delta`` names defensively).

The adapter surface — ``connect`` / ``send_audio`` / ``receive_events`` /
``send_text`` / ``send_function_result`` / ``cancel_response`` / ``close`` —
matches :class:`~getpatter.providers.openai_realtime.OpenAIRealtimeAdapter`
(and the Ultravox / Gemini Live adapters), so callers can swap providers
without touching the handler.

Pricing: $0.05/min of session time plus $0.004 per client
``conversation.item.create`` text message. See
``DEFAULT_PRICING["xai_realtime"]``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections import deque
from enum import IntEnum, StrEnum
from typing import Any, AsyncIterator, Literal, Union

import websockets

logger = logging.getLogger("getpatter.xai_realtime")

XAI_REALTIME_URL = "wss://api.x.ai/v1/realtime"
XAI_CLIENT_SECRETS_URL = "https://api.x.ai/v1/realtime/client_secrets"


class XAIRealtimeModel(StrEnum):
    """Known xAI Voice Agent model identifiers.

    ``GROK_VOICE_LATEST`` always points at the newest model (currently
    ``grok-voice-think-fast-1.0``). ``GROK_VOICE_FAST_1_0`` is the legacy
    (deprecated) voice model.
    """

    GROK_VOICE_LATEST = "grok-voice-latest"
    GROK_VOICE_THINK_FAST_1_0 = "grok-voice-think-fast-1.0"
    GROK_VOICE_FAST_1_0 = "grok-voice-fast-1.0"


class XAIRealtimeAudioFormat(StrEnum):
    """Audio wire formats accepted by the xAI Voice Agent API.

    ``AUDIO_PCM`` is Linear16 little-endian at a configurable sample rate;
    ``AUDIO_PCMU`` / ``AUDIO_PCMA`` are G.711 μ-law / A-law pinned to 8 kHz
    (carrier-native for Twilio / Telnyx / Plivo — zero transcode).
    """

    AUDIO_PCM = "audio/pcm"
    AUDIO_PCMU = "audio/pcmu"
    AUDIO_PCMA = "audio/pcma"


class XAIRealtimeSampleRate(IntEnum):
    """PCM sample rates accepted by the Voice Agent API (``audio/pcm`` only;
    the G.711 formats are fixed at 8 kHz)."""

    HZ_8000 = 8000
    HZ_16000 = 16000
    HZ_22050 = 22050
    HZ_24000 = 24000
    HZ_32000 = 32000
    HZ_44100 = 44100
    HZ_48000 = 48000


class XAIRealtimeToolType(StrEnum):
    """Tool types accepted in the ``session.tools`` array."""

    FUNCTION = "function"
    WEB_SEARCH = "web_search"
    X_SEARCH = "x_search"
    FILE_SEARCH = "file_search"
    MCP = "mcp"


class XAIRealtimeAdapter:
    """Bridges a Twilio/Telnyx media stream to the xAI Voice Agent API.

    Handles the full conversation loop: audio in → Grok → audio out.
    No separate STT/TTS needed.
    """

    _SESSION_UPDATE_TIMEOUT = 5.0

    def __init__(
        self,
        api_key: str,
        model: Union[XAIRealtimeModel, str] = XAIRealtimeModel.GROK_VOICE_LATEST,
        voice: str = "eve",
        instructions: str = "",
        language: str = "en",
        tools: list[dict] | None = None,
        audio_format: Union[
            XAIRealtimeAudioFormat, str
        ] = XAIRealtimeAudioFormat.AUDIO_PCMU,
        sample_rate: Union[XAIRealtimeSampleRate, int] = XAIRealtimeSampleRate.HZ_8000,
        *,
        reasoning_effort: Literal["high", "none"] | None = None,
        vad_threshold: float | None = None,
        silence_duration_ms: int | None = None,
        prefix_padding_ms: int | None = None,
        idle_timeout_ms: int | None = None,
        language_hint: str | None = None,
        keyterms: list[str] | None = None,
        output_speed: float | None = None,
        replace: dict[str, str] | None = None,
        input_transcription_model: str | None = None,
    ):
        # Default wire format is G.711 μ-law @ 8 kHz — matches the
        # Twilio/Telnyx inbound codec so audio flows through without
        # transcoding (parity with the OpenAI Realtime adapter default).
        #
        # ``voice`` accepts any built-in voice (see
        # :class:`~getpatter.providers.xai_tts.XAIVoice`) or a custom voice
        # ID cloned via the Custom Voices API.
        #
        # ``replace`` maps model-output words to replacement pronunciations
        # (case-insensitive whole-word match); only the spoken audio changes,
        # the transcript keeps the original text.
        #
        # ``input_transcription_model``: set to ``"grok-transcribe"`` to
        # receive streaming ``conversation.item.input_audio_transcription.updated``
        # events (live captions) in addition to the final ``.completed``.
        if output_speed is not None and not 0.7 <= output_speed <= 1.5:
            raise ValueError(
                f"output_speed must be in [0.7, 1.5], got {output_speed}"
            )
        self.api_key = api_key
        self.model = model
        self.voice = voice
        self.instructions = instructions
        self.language = language
        self.tools = tools
        self.audio_format = audio_format
        self.sample_rate = sample_rate
        self.reasoning_effort = reasoning_effort
        self.vad_threshold = vad_threshold
        self.silence_duration_ms = silence_duration_ms
        self.prefix_padding_ms = prefix_padding_ms
        self.idle_timeout_ms = idle_timeout_ms
        self.language_hint = language_hint
        self.keyterms = keyterms
        self.output_speed = output_speed
        self.replace = replace
        self.input_transcription_model = input_transcription_model
        self._ws: Any = None
        self._running = False
        # Track the assistant message currently being generated so we can
        # truncate it cleanly on barge-in.
        self._current_response_item_id: str | None = None
        self._current_response_audio_ms: int = 0
        # Wall-clock timestamp of the first audio delta of the current
        # response — bounds ``audio_end_ms`` on truncate to what the caller
        # could plausibly have heard (generation runs 5-10x real-time).
        self._current_response_first_audio_at: float | None = None
        # Messages read during the ``session.updated`` ack wait get buffered
        # here and drained by ``receive_events`` before reading the socket.
        self._pending_events: deque[str] = deque()
        # Session start time for ``patter.cost.realtime_minutes`` emission
        # on close — xAI bills realtime per minute of session time.
        self._session_start_monotonic: float = time.monotonic()
        # Count of client conversation.item.create messages — billed at
        # $0.004/message on top of the per-minute rate.
        self._text_input_messages: int = 0

    def __repr__(self) -> str:
        return (
            f"XAIRealtimeAdapter(model={self.model!r}, voice={self.voice!r}, "
            f"audio_format={self.audio_format!r}, sample_rate={self.sample_rate})"
        )

    def record_session_end(self) -> None:
        """Emit ``patter.cost.realtime_minutes`` (and the text-input message
        count) for the elapsed session duration."""
        try:
            from getpatter.observability.attributes import record_patter_attrs

            elapsed = time.monotonic() - self._session_start_monotonic
            record_patter_attrs(
                {
                    "patter.cost.realtime_minutes": elapsed / 60.0,
                    "patter.cost.realtime_text_messages": self._text_input_messages,
                    "patter.realtime.provider": "xai_realtime",
                }
            )
        except Exception:  # pragma: no cover — defense in depth
            logger.debug("record_session_end failed", exc_info=True)

    # ------------------------------------------------------------------
    # Ephemeral tokens (browser-side auth)
    # ------------------------------------------------------------------

    @classmethod
    async def create_ephemeral_token(
        cls,
        api_key: str,
        *,
        expires_seconds: int = 600,
        session: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a short-lived client secret via
        ``POST /v1/realtime/client_secrets`` for browser/mobile connections.

        The returned ``value`` is passed by the client either as a Bearer
        token or in the WebSocket subprotocol as
        ``xai-client-secret.<token>`` (browsers cannot set headers).
        ``expires_seconds`` is capped at 3600 by the API. ``session``
        optionally binds an initial session configuration (``model``,
        ``reasoning``) to the secret.
        """
        import httpx

        payload: dict[str, Any] = {"expires_after": {"seconds": expires_seconds}}
        if session is not None:
            payload["session"] = session
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                XAI_CLIENT_SECRETS_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            return resp.json()

    # ------------------------------------------------------------------
    # Session config
    # ------------------------------------------------------------------

    @staticmethod
    def _build_tool_wire_format(tool: dict) -> dict:
        """Build the xAI Realtime tool envelope.

        Server-side tools (``web_search``, ``x_search``, ``file_search``,
        ``mcp``) already carry an explicit ``type`` and pass through
        unchanged. Patter ``Tool`` dicts (name/description/parameters) are
        wrapped in the ``function`` envelope shared with OpenAI Realtime.
        """
        tool_type = tool.get("type")
        if tool_type and tool_type != XAIRealtimeToolType.FUNCTION:
            return dict(tool)
        return {
            "type": "function",
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
        }

    def _build_audio_format(self) -> dict[str, Any]:
        fmt: dict[str, Any] = {"type": str(self.audio_format)}
        # ``rate`` only applies to audio/pcm; the G.711 formats are pinned
        # to 8 kHz server-side.
        if self.audio_format == XAIRealtimeAudioFormat.AUDIO_PCM:
            fmt["rate"] = int(self.sample_rate)
        return fmt

    def _build_session_config(self) -> dict[str, Any]:
        """Build the ``session.update`` body shared by ``connect`` and
        ``warmup`` so both paths prime the upstream session identically."""
        turn_detection: dict[str, Any] = {"type": "server_vad"}
        if self.vad_threshold is not None:
            turn_detection["threshold"] = self.vad_threshold
        if self.silence_duration_ms is not None:
            turn_detection["silence_duration_ms"] = self.silence_duration_ms
        if self.prefix_padding_ms is not None:
            turn_detection["prefix_padding_ms"] = self.prefix_padding_ms
        if self.idle_timeout_ms is not None:
            turn_detection["idle_timeout_ms"] = self.idle_timeout_ms

        audio_format = self._build_audio_format()
        audio: dict[str, Any] = {
            "input": {"format": dict(audio_format)},
            "output": {"format": dict(audio_format)},
        }
        transcription: dict[str, Any] = {}
        if self.language_hint is not None:
            transcription["language_hint"] = self.language_hint
        if self.keyterms:
            transcription["keyterms"] = list(self.keyterms)
        if self.input_transcription_model is not None:
            transcription["model"] = self.input_transcription_model
        if transcription:
            audio["input"]["transcription"] = transcription
        if self.output_speed is not None:
            audio["output"]["speed"] = self.output_speed

        session_config: dict[str, Any] = {
            "voice": self.voice,
            "instructions": self.instructions
            or f"You are a helpful voice assistant. Respond in {self.language}. "
            "Be concise and natural.",
            "turn_detection": turn_detection,
            "audio": audio,
        }
        if self.reasoning_effort is not None:
            session_config["reasoning"] = {"effort": self.reasoning_effort}
        if self.replace:
            session_config["replace"] = dict(self.replace)
        if self.tools:
            session_config["tools"] = [
                self._build_tool_wire_format(t) for t in self.tools
            ]
        return session_config

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def _build_url(self) -> str:
        return f"{XAI_REALTIME_URL}?model={self.model}"

    async def warmup(self) -> None:
        """Pre-call WebSocket warmup for the xAI Realtime endpoint.

        Opens the WS, waits for ``session.created``, applies the same
        ``session.update`` the production :meth:`connect` path sends, waits
        for the ``session.updated`` ack, then closes cleanly.

        Billing safety: xAI bills realtime sessions per minute of
        *conversation* time and per ``conversation.item.create`` text
        message. ``session.update`` neither invokes the model nor creates a
        conversation item, and the socket is closed within ~1 s. Best-effort:
        failures are logged at DEBUG and never raised.
        """
        ws = None
        try:
            ws = await asyncio.wait_for(
                websockets.connect(
                    self._build_url(),
                    additional_headers={"Authorization": f"Bearer {self.api_key}"},
                    ping_interval=20,
                    ping_timeout=20,
                ),
                timeout=5.0,
            )
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                data = json.loads(raw)
                # The first frames on connect are session.created /
                # conversation.created (order not guaranteed) — accept either.
                if data.get("type") not in ("session.created", "conversation.created"):
                    return
            except Exception:
                return
            try:
                await ws.send(
                    json.dumps(
                        {
                            "type": "session.update",
                            "session": self._build_session_config(),
                        }
                    )
                )
            except Exception:
                return
            deadline = asyncio.get_event_loop().time() + 1.5
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    data = json.loads(raw)
                except Exception:
                    break
                if isinstance(data, dict) and data.get("type") == "session.updated":
                    break
        except Exception as exc:  # noqa: BLE001 - best-effort
            logger.debug("xAI Realtime warmup failed (best-effort): %s", exc)
        finally:
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass

    async def connect(self) -> None:
        """Connect to the xAI Voice Agent API and wait for ``session.updated``."""
        self._ws = await websockets.connect(
            self._build_url(),
            additional_headers={"Authorization": f"Bearer {self.api_key}"},
            # Keep the connection alive on long conversational pauses; a
            # dropped WS mid-call is the single most common failure on
            # carrier links with aggressive NAT timeouts.
            ping_interval=20,
            ping_timeout=20,
        )
        self._running = True

        try:
            # On connect the server sends session.created and
            # conversation.created (in either order). Wait for the session
            # frame, tolerating the conversation frame.
            deadline = asyncio.get_event_loop().time() + 15.0
            session_created = False
            while not session_created:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    raise RuntimeError(
                        "xAI Realtime: no session.created within 15s"
                    )
                raw = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
                data = json.loads(raw)
                msg_type = data.get("type")
                if msg_type == "session.created":
                    session_created = True
                elif msg_type == "conversation.created":
                    continue
                elif msg_type == "error":
                    err = data.get("error") or {}
                    msg = err.get("message") or err.get("code") or str(data)
                    raise RuntimeError(f"xAI Realtime setup error: {msg}")
                else:
                    # Unknown pre-session frame — buffer for the receive loop.
                    self._pending_events.append(raw)

            await self._ws.send(
                json.dumps(
                    {
                        "type": "session.update",
                        "session": self._build_session_config(),
                    }
                )
            )

            # Wait for the ``session.updated`` ack before allowing any
            # audio / text traffic — without it the first turn races the
            # config exchange.
            await self._await_session_updated()
        except Exception:
            await self._ws.close()
            self._ws = None
            self._running = False
            raise

    async def _await_session_updated(self) -> None:
        """Drain frames until ``session.updated`` (bounded); non-matching
        frames are buffered for the normal receive loop."""
        deadline = asyncio.get_event_loop().time() + self._SESSION_UPDATE_TIMEOUT
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                logger.warning(
                    "xAI Realtime: no session.updated after %.1fs; continuing anyway",
                    self._SESSION_UPDATE_TIMEOUT,
                )
                return
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
            except (TimeoutError, asyncio.TimeoutError):
                logger.warning(
                    "xAI Realtime: no session.updated after %.1fs; continuing anyway",
                    self._SESSION_UPDATE_TIMEOUT,
                )
                return
            try:
                data = json.loads(raw)
            except Exception:  # pragma: no cover — malformed JSON
                continue
            if data.get("type") == "session.updated":
                return
            self._pending_events.append(raw)

    # ------------------------------------------------------------------
    # Client → server
    # ------------------------------------------------------------------

    async def send_audio(self, audio: bytes) -> None:
        """Append base64 audio to the input buffer (format must match the
        configured ``audio_format`` / ``sample_rate``)."""
        if self._ws is None:
            return
        encoded = base64.b64encode(audio).decode("ascii")
        await self._ws.send(
            json.dumps({"type": "input_audio_buffer.append", "audio": encoded})
        )

    async def send_text(self, text: str) -> None:
        """Send a user text message (triggers a spoken response).

        Note: xAI bills each client ``conversation.item.create`` at
        $0.004/message on top of the per-minute session rate.
        """
        if self._ws is None:
            return
        self._text_input_messages += 1
        await self._ws.send(
            json.dumps(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": text}],
                    },
                }
            )
        )
        await self._ws.send(json.dumps({"type": "response.create"}))

    async def send_first_message(self, text: str) -> None:
        """Make the agent speak ``text`` verbatim as its opening line.

        Seeds the greeting as an ASSISTANT history item and triggers a
        response — injecting it as ``role: user`` would make the model
        *reply to* its own greeting (role-confused openings).
        """
        if self._ws is None:
            return
        self._text_input_messages += 1
        await self._ws.send(
            json.dumps(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "text", "text": text}],
                    },
                }
            )
        )
        await self._ws.send(
            json.dumps(
                {
                    "type": "response.create",
                    "response": {
                        "instructions": (
                            f"Say exactly the following sentence as your first "
                            f'turn and nothing else: "{text}"'
                        ),
                    },
                }
            )
        )

    async def request_response(self) -> None:
        """Trigger ``response.create`` with no new user item."""
        if self._ws is None:
            return
        await self._ws.send(json.dumps({"type": "response.create"}))

    async def send_function_result(self, call_id: str, result: str) -> None:
        """Return a function call result and trigger a new response."""
        if self._ws is None:
            return
        await self._ws.send(
            json.dumps(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": result,
                    },
                }
            )
        )
        await self._ws.send(json.dumps({"type": "response.create"}))

    async def update_session(
        self,
        *,
        instructions: str | None = None,
        tools: list[dict] | None = None,
        language_hint: str | None = None,
        keyterms: list[str] | None = None,
        replace: dict[str, str] | None = None,
    ) -> None:
        """Apply a mid-session config swap (multi-agent handoff, ASR biasing).

        Sends a partial ``session.update`` carrying only the supplied fields
        and records them on the adapter so reconnect/warmup paths rebuild the
        session with the post-swap config. The server merges partial updates.
        """
        session: dict[str, Any] = {}
        if instructions is not None:
            self.instructions = instructions
            session["instructions"] = instructions
        if tools is not None:
            self.tools = tools
            session["tools"] = [self._build_tool_wire_format(t) for t in tools]
        transcription: dict[str, Any] = {}
        if language_hint is not None:
            self.language_hint = language_hint
            transcription["language_hint"] = language_hint
        if keyterms is not None:
            self.keyterms = keyterms
            transcription["keyterms"] = list(keyterms)
        if transcription:
            session["audio"] = {"input": {"transcription": transcription}}
        if replace is not None:
            self.replace = replace
            session["replace"] = dict(replace)
        if not session or self._ws is None:
            return
        await self._ws.send(
            json.dumps({"type": "session.update", "session": session})
        )

    # ------------------------------------------------------------------
    # Barge-in
    # ------------------------------------------------------------------

    async def truncate_playback(self) -> None:
        """Truncate the in-flight assistant item to what the caller heard.

        Sends ``conversation.item.truncate`` with ``audio_end_ms`` bounded by
        wall-clock time since the first audio delta — generation runs 5-10x
        real-time, so the byte-derived counter overstates what a 1x playback
        could have produced. No-op when no response is in flight.
        """
        if self._ws is None or not self._current_response_item_id:
            return
        audio_end_ms = self._current_response_audio_ms
        if self._current_response_first_audio_at is not None:
            elapsed_ms = int(
                (time.monotonic() - self._current_response_first_audio_at) * 1000
            )
            audio_end_ms = min(audio_end_ms, max(elapsed_ms, 0))
        try:
            await self._ws.send(
                json.dumps(
                    {
                        "type": "conversation.item.truncate",
                        "item_id": self._current_response_item_id,
                        "content_index": 0,
                        "audio_end_ms": audio_end_ms,
                    }
                )
            )
        except Exception as exc:  # pragma: no cover
            logger.debug("conversation.item.truncate failed: %s", exc)
        self._current_response_item_id = None
        self._current_response_audio_ms = 0
        self._current_response_first_audio_at = None

    async def cancel_response(self) -> None:
        """Cancel the current response AND truncate the in-flight item.

        In server-VAD mode xAI interrupts automatically on barge-in; this
        explicit path covers manual cancels (guardrail rewrites, transfer /
        hangup). Idempotent: no-ops when no response is in flight.
        """
        if self._ws is None or not self._current_response_item_id:
            return
        await self.truncate_playback()
        try:
            await self._ws.send(json.dumps({"type": "response.cancel"}))
        except Exception as exc:
            logger.debug("response.cancel failed: %s", exc)

    # ------------------------------------------------------------------
    # Server → client
    # ------------------------------------------------------------------

    async def receive_events(self) -> AsyncIterator[tuple[str, Any]]:
        """Yield ``(event_type, payload)`` tuples from the Voice Agent API.

        Yields:
            ``("audio", bytes)`` — audio chunk in the configured wire format
            ``("transcript_input", str)`` — what the user said (final)
            ``("transcript_output", str)`` — agent transcript delta
            ``("speech_started", None)`` — user began speaking (barge-in)
            ``("speech_stopped", None)`` — user stopped speaking
            ``("function_call", {"call_id", "name", "arguments"})``
            ``("response_done", dict)`` — agent finished the current turn
            ``("error", dict)`` — surfaced error from the server / transport
        """
        if self._ws is None:
            return

        async def _iter_raw():
            while self._pending_events:
                yield self._pending_events.popleft()
            async for msg in self._ws:
                yield msg

        try:
            async for raw in _iter_raw():
                try:
                    data = json.loads(raw)
                except Exception:
                    continue
                event_type = data.get("type", "")

                # GA name plus legacy alias — xAI emits the GA
                # ``response.output_audio.delta``.
                if event_type in (
                    "response.output_audio.delta",
                    "response.audio.delta",
                ):
                    audio_bytes = base64.b64decode(data.get("delta", ""))
                    self._current_response_audio_ms += _estimate_audio_ms(
                        audio_bytes, str(self.audio_format), int(self.sample_rate)
                    )
                    if self._current_response_first_audio_at is None:
                        self._current_response_first_audio_at = time.monotonic()
                    yield ("audio", audio_bytes)

                elif event_type in (
                    "response.output_audio_transcript.delta",
                    "response.audio_transcript.delta",
                ):
                    yield ("transcript_output", data.get("delta", ""))

                elif event_type in (
                    "response.content_part.added",
                    "response.output_item.added",
                ):
                    # Track the in-flight assistant item id for barge-in
                    # truncation; skip function_call items (truncating one
                    # makes the server reject with an error event).
                    item = data.get("item") or {}
                    item_type = item.get("type")
                    item_id = item.get("id") or data.get("item_id")
                    if item_id and item_type in (None, "message"):
                        self._current_response_item_id = item_id
                        self._current_response_audio_ms = 0
                        self._current_response_first_audio_at = None

                elif event_type == "input_audio_buffer.speech_started":
                    yield ("speech_started", None)

                elif event_type == "input_audio_buffer.speech_stopped":
                    yield ("speech_stopped", None)

                elif (
                    event_type
                    == "conversation.item.input_audio_transcription.completed"
                ):
                    yield ("transcript_input", data.get("transcript", ""))

                elif event_type == "response.function_call_arguments.done":
                    yield (
                        "function_call",
                        {
                            "call_id": data.get("call_id", ""),
                            "name": data.get("name", ""),
                            "arguments": data.get("arguments", "{}"),
                        },
                    )

                elif event_type == "response.done":
                    self._current_response_item_id = None
                    self._current_response_audio_ms = 0
                    self._current_response_first_audio_at = None
                    yield ("response_done", data.get("response", {}))

                elif event_type == "error":
                    err = data.get("error", {}) or {
                        "message": data.get("message", "")
                    }
                    logger.error("xAI Realtime error: %s", err)
                    yield ("error", err)

        except websockets.exceptions.ConnectionClosed as exc:
            if self._running and getattr(exc, "code", 1000) != 1000:
                yield (
                    "error",
                    {
                        "type": "connection_closed",
                        "code": getattr(exc, "code", None),
                        "reason": getattr(exc, "reason", ""),
                    },
                )
        finally:
            self._running = False

    async def close(self) -> None:
        """Close the connection and record the billable session duration."""
        self.record_session_end()
        self._running = False
        if self._ws:
            await self._ws.close()
            self._ws = None


def _estimate_audio_ms(chunk: bytes, audio_format: str, sample_rate: int) -> int:
    """Rough audio duration estimate used for truncation accounting.

    - ``audio/pcmu`` / ``audio/pcma``: 8 kHz, 1 byte/sample → ms = bytes/8
    - ``audio/pcm``: 2 bytes/sample at the configured rate →
      ms = bytes / (rate * 2 / 1000)
    """
    if not chunk:
        return 0
    if audio_format in (
        XAIRealtimeAudioFormat.AUDIO_PCMU.value,
        XAIRealtimeAudioFormat.AUDIO_PCMA.value,
    ):
        return len(chunk) // 8
    if audio_format == XAIRealtimeAudioFormat.AUDIO_PCM.value:
        bytes_per_ms = max(int(sample_rate) * 2 // 1000, 1)
        return len(chunk) // bytes_per_ms
    return 0
