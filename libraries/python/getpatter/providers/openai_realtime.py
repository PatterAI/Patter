"""OpenAI Realtime API adapter — all-in-one STT + LLM + TTS over WebSocket.

Drives :class:`OpenAIRealtimeAdapter`, which negotiates audio format,
dispatches tool calls, and streams audio in both directions.

NOTE (issue #154): the ``openai_realtime`` provider mode no longer uses this
adapter's standalone connect path for telephony. OpenAI deprecated the Beta
Realtime API, so its flat ``output_audio_format: g711_ulaw`` session shape is
ignored by GA models (the server falls back to PCM16 @ 24 kHz). ``stream_handler``
routes BOTH ``openai_realtime`` and ``openai_realtime_2`` through
:class:`~getpatter.providers.openai_realtime_2.OpenAIRealtime2Adapter` (GA
session shape + internal PCM24->mulaw8 transcode). This class is retained as the
shared base class that ``OpenAIRealtime2Adapter`` extends.
"""

import asyncio
import base64
import json
import logging
import time
from collections import deque
from enum import StrEnum
from typing import Any, Literal

import websockets

logger = logging.getLogger("getpatter.openai_realtime")


class OpenAIRealtimeModel(StrEnum):
    """Known OpenAI Realtime API model identifiers.

    ``GPT_REALTIME_2`` is OpenAI's most-capable realtime voice model
    (speech-to-speech with configurable reasoning effort, stronger
    instruction following, 128K context). It accepts the same session
    update wire format as the v1 ``gpt-realtime`` family but supports an
    additional ``reasoning.effort`` field — see ``reasoning_effort`` on
    :class:`OpenAIRealtimeAdapter`. Pricing differs from the mini default;
    override ``DEFAULT_PRICING["openai_realtime"]`` with the values in
    ``DEFAULT_PRICING["openai_realtime_2"]`` when selecting it.
    """

    GPT_REALTIME = "gpt-realtime"
    GPT_REALTIME_2 = "gpt-realtime-2"
    GPT_REALTIME_MINI = "gpt-realtime-mini"
    GPT_4O_REALTIME_PREVIEW = "gpt-4o-realtime-preview"
    GPT_4O_MINI_REALTIME_PREVIEW = "gpt-4o-mini-realtime-preview"


class OpenAIVoice(StrEnum):
    """OpenAI Realtime / TTS voice identifiers."""

    ALLOY = "alloy"
    ASH = "ash"
    BALLAD = "ballad"
    CORAL = "coral"
    ECHO = "echo"
    FABLE = "fable"
    NOVA = "nova"
    ONYX = "onyx"
    SAGE = "sage"
    SHIMMER = "shimmer"
    VERSE = "verse"


class OpenAIRealtimeAudioFormat(StrEnum):
    """Supported audio formats on the OpenAI Realtime API."""

    PCM16 = "pcm16"
    G711_ULAW = "g711_ulaw"
    G711_ALAW = "g711_alaw"


class OpenAITranscriptionModel(StrEnum):
    """Models accepted by ``input_audio_transcription`` on Realtime sessions.

    ``GPT_REALTIME_WHISPER`` is OpenAI's streaming-optimised Whisper variant
    designed for low-latency transcript deltas inside a Realtime session.
    Billed per minute of audio (separate from the conversational model
    tokens). Use it when you want faster partial transcripts than
    ``whisper-1`` at lower cost than ``gpt-4o-transcribe``.
    """

    WHISPER_1 = "whisper-1"
    GPT_4O_TRANSCRIBE = "gpt-4o-transcribe"
    GPT_4O_MINI_TRANSCRIBE = "gpt-4o-mini-transcribe"
    GPT_REALTIME_WHISPER = "gpt-realtime-whisper"


class OpenAIRealtimeVADType(StrEnum):
    """Server-side voice-activity-detection modes."""

    SERVER_VAD = "server_vad"
    SEMANTIC_VAD = "semantic_vad"


def build_turn_detection(
    turn_detection: Any,
    *,
    default_type: str,
    default_silence_ms: int,
    include_response_gating: bool,
    gate_response_on_transcript: bool = False,
) -> dict[str, Any]:
    """Build the ``turn_detection`` wire dict shared by the v1 and GA session
    builders so the two paths never drift.

    ``turn_detection`` is an optional :class:`getpatter.models.RealtimeTurnDetection`
    (duck-typed — only its ``type`` / ``threshold`` / ``prefix_padding_ms`` /
    ``silence_duration_ms`` / ``eagerness`` attributes are read). When ``None``
    the adapter's current hardcoded defaults are emitted. ``semantic_vad`` omits
    threshold / padding / silence (OpenAI rejects them) and emits ``eagerness``
    only when set.

    ``include_response_gating`` adds the GA-only ``create_response`` /
    ``interrupt_response`` keys. The v1 shape omits them (the server's
    true-defaults apply).

    ``gate_response_on_transcript`` selects who owns turn-taking and is the
    single switch both gating keys are tied to:

    - ``False`` (DEFAULT — server-managed): ``create_response`` and
      ``interrupt_response`` are BOTH ``True``. The OpenAI server owns VAD,
      end-of-turn, response creation, and the barge-in cancel signal. On a
      WebSocket transport the client still clears its playout buffer and sends
      ``conversation.item.truncate`` on the server's ``speech_started`` — the
      server does not auto-truncate over WebSocket — but it does NOT send
      ``response.cancel`` (the server already cancels via ``interrupt_response``)
      and it does NOT drive ``response.create``.
    - ``True`` (OPT-OUT — legacy client-managed): both keys are ``False``. The
      stream-handler drives ``response.create`` after the hallucination filter
      and runs the full client-side barge-in path (``cancel_response`` +
      MIN_AGENT_SPEAKING gate). This is the escape hatch for no-AEC PSTN
      self-interruption.
    """
    td = turn_detection
    if td is not None and getattr(td, "type", "server_vad") == "semantic_vad":
        detection: dict[str, Any] = {"type": "semantic_vad"}
        eagerness = getattr(td, "eagerness", None)
        if eagerness is not None:
            detection["eagerness"] = eagerness
    else:
        td_type = getattr(td, "type", None) if td is not None else None
        threshold = getattr(td, "threshold", None) if td is not None else None
        prefix = getattr(td, "prefix_padding_ms", None) if td is not None else None
        silence = getattr(td, "silence_duration_ms", None) if td is not None else None
        detection = {
            "type": td_type or default_type,
            "threshold": threshold if threshold is not None else 0.5,
            "prefix_padding_ms": prefix if prefix is not None else 300,
            "silence_duration_ms": silence
            if silence is not None
            else default_silence_ms,
        }
    if include_response_gating:
        # Both keys are tied to the same switch: server-managed (the default,
        # gate=False) => both True; client-managed legacy (gate=True) =>
        # both False.
        server_managed = not gate_response_on_transcript
        detection["create_response"] = server_managed
        detection["interrupt_response"] = server_managed
    return detection


class OpenAIRealtimeAdapter:
    """Bridges Twilio/Telnyx media stream to OpenAI Realtime API.

    Handles the full conversation loop: audio in → AI processing → audio out.
    No separate STT/TTS needed.
    """

    OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime"
    _SESSION_UPDATE_TIMEOUT = 5.0

    def __init__(
        self,
        api_key: str,
        model: OpenAIRealtimeModel | str = OpenAIRealtimeModel.GPT_REALTIME_MINI,
        voice: OpenAIVoice | str = OpenAIVoice.ALLOY,
        instructions: str = "",
        language: str = "en",
        tools: list[dict] | None = None,
        audio_format: OpenAIRealtimeAudioFormat
        | str = OpenAIRealtimeAudioFormat.G711_ULAW,
        *,
        temperature: float | None = None,
        max_response_output_tokens: int | str | None = None,
        modalities: list[str] | None = None,
        tool_choice: str | dict | None = None,
        input_audio_transcription_model: OpenAITranscriptionModel
        | str = OpenAITranscriptionModel.WHISPER_1,
        vad_type: Literal[
            "server_vad", "semantic_vad"
        ] = OpenAIRealtimeVADType.SERVER_VAD.value,
        # OpenAI's documented sweet-spot for snappier turns. Lowering from the
        # previous 500 ms saves ~200 ms per turn end. Override via constructor
        # if a use case (e.g. dictation) needs more trailing silence.
        silence_duration_ms: int = 300,
        # Reasoning-effort tier for ``gpt-realtime-2``. None leaves the field
        # unset (server default). OpenAI recommends ``"low"`` for production
        # voice flows — higher tiers add measurable per-turn latency.
        reasoning_effort: Literal["minimal", "low", "medium", "high"] | None = None,
        # Input noise reduction for speakerphone / conference audio. ``None``
        # (default) omits the field (no reduction — today's behavior).
        noise_reduction: Literal["near_field", "far_field"] | None = None,
        # Turn-detection tuning. ``None`` (default) keeps the adapter's current
        # hardcoded turn_detection. Typed ``Any`` to avoid importing
        # ``RealtimeTurnDetection`` into the provider module (duck-typed at
        # use: ``.type`` / ``.threshold`` / ``.eagerness`` / ...).
        turn_detection: Any | None = None,
        # When ``True``, the stream-handler gates the model response on the
        # Whisper ``input_audio_transcription.completed`` event (legacy
        # behavior). ``None``/``False`` (default) decouples the response: the
        # handler triggers ``response.create`` as soon as the user stops
        # speaking, so the model no longer waits ~500 ms for Whisper. Read by
        # the stream-handler off the adapter; the adapter itself does not gate.
        gate_response_on_transcript: bool | None = None,
    ):
        if noise_reduction is not None and noise_reduction not in (
            "near_field",
            "far_field",
        ):
            raise ValueError(
                "noise_reduction must be 'near_field' or 'far_field', "
                f"got {noise_reduction!r}"
            )
        self.api_key = api_key
        self.model = model
        self.voice = voice
        self.instructions = instructions
        self.language = language
        self.tools = tools
        self.audio_format = audio_format
        self.temperature = temperature
        self.max_response_output_tokens = max_response_output_tokens
        self.modalities = modalities
        self.tool_choice = tool_choice
        self.input_audio_transcription_model = input_audio_transcription_model
        self.vad_type = vad_type
        self.silence_duration_ms = silence_duration_ms
        self.reasoning_effort = reasoning_effort
        self.noise_reduction = noise_reduction
        self.turn_detection = turn_detection
        # Decouple flag: ``False`` (default) means the stream-handler fires
        # ``response.create`` on speech-stop (committed), NOT on the Whisper
        # transcript. ``True`` restores the legacy transcript-gated path.
        self.gate_response_on_transcript = (
            gate_response_on_transcript
            if gate_response_on_transcript is not None
            else False
        )
        self._ws: Any = None
        self._running = False
        # Track the assistant message currently being generated so we can
        # truncate it cleanly on barge-in (see ``input_audio_buffer.speech_started``).
        self._current_response_item_id: str | None = None
        self._current_response_audio_ms: int = 0
        # Wall-clock timestamp (``time.monotonic()``) of the first
        # ``response.audio.delta`` received since the current response item
        # started. Used by ``cancel_response`` to bound ``audio_end_ms`` to
        # what the caller could plausibly have heard — generated audio
        # frequently arrives at 5-10x real-time, so ``audio_end_ms`` driven
        # purely by the per-chunk byte counter overshoots reality and leaves
        # phantom assistant text on the conversation. The wall-clock cap
        # corresponds to the maximum playback that real-time TTS could have
        # produced, which is what the user actually heard.
        self._current_response_first_audio_at: float | None = None
        # Messages read during the ``session.updated`` ack wait get buffered
        # here and drained by ``receive_events`` before reading the socket.
        self._pending_events: deque[str] = deque()
        self._receive_task: asyncio.Task | None = None
        # Session start time for ``patter.cost.realtime_minutes`` emission on close.
        import time as _time

        self._session_start_monotonic: float = _time.monotonic()

    def record_session_end(self) -> None:
        """Emit ``patter.cost.realtime_minutes`` for the elapsed session duration."""
        try:
            import time as _time

            from getpatter.observability.attributes import record_patter_attrs

            elapsed = _time.monotonic() - self._session_start_monotonic
            record_patter_attrs(
                {
                    "patter.cost.realtime_minutes": elapsed / 60.0,
                    "patter.realtime.provider": "openai_realtime",
                }
            )
        except Exception:  # pragma: no cover — defense in depth
            logger.debug("record_session_end failed", exc_info=True)

    def __repr__(self) -> str:
        return f"OpenAIRealtimeAdapter(model={self.model!r}, voice={self.voice!r}, audio_format={self.audio_format!r})"

    @staticmethod
    def _build_tool_wire_format(tool: dict) -> dict:
        """Build the OpenAI Realtime function-tool envelope. Propagates
        ``strict: true`` only when the user opted in — Patter does not flip
        it on by default because OpenAI strict mode requires every property
        in ``required`` and ``additionalProperties: false`` everywhere,
        which would break tools with optional fields. Schema validation
        runs at ``Patter.agent(...)`` build time so any strict-mode
        violation is surfaced before this wire format is sent."""
        wire: dict = {
            "type": "function",
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["parameters"],
        }
        if tool.get("strict") is True:
            wire["strict"] = True
        return wire

    async def warmup(self) -> None:
        """Pre-call WebSocket warmup for the OpenAI Realtime endpoint.

        The canonical session-only warm step on the Realtime API: open
        the WS, wait for ``session.created``, send a single
        ``session.update`` containing the same fields that the production
        ``connect()`` path applies (``input_audio_format``,
        ``output_audio_format``, ``voice``, ``instructions``,
        ``turn_detection``, ``input_audio_transcription``, plus any
        opt-in fields populated on the adapter), wait for the matching
        ``session.updated`` ack, then close cleanly. This primes the
        per-session state on the OpenAI side — DNS + TLS + auth handshake
        + initial config exchange — without ever invoking the model.

        Earlier revisions sent ``response.create`` with
        ``{"response": {"generate": false}}`` to prime the inference path.
        That field is NOT in the OpenAI Realtime API schema; the server
        either ignores it (and bills tokens for a real model response) or
        rejects the request with ``invalid_request_error``. Both
        behaviours are billing-unsafe or a no-op beyond TLS warm. The
        ``session.update`` flow is documented and side-effect-free.

        Billing safety: ``session.update`` only mutates session
        configuration. It does NOT invoke the model, does NOT consume
        any audio buffer, and does NOT trigger token generation, so no
        per-token cost is accrued. Best-effort: failures are logged at
        DEBUG and never raised.
        """
        url = f"{self.OPENAI_REALTIME_URL}?model={self.model}"
        ws = None
        try:
            ws = await asyncio.wait_for(
                websockets.connect(
                    url,
                    additional_headers={
                        "Authorization": f"Bearer {self.api_key}",
                    },
                    ping_interval=20,
                    ping_timeout=20,
                ),
                timeout=5.0,
            )
            # Wait for session.created.
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                data = json.loads(raw)
                if data.get("type") != "session.created":
                    # Anything else is unexpected but not fatal — we close.
                    return
            except Exception:
                return
            # Send session.update with the same fields the production
            # ``connect()`` path applies, so the upstream session state is
            # primed identically to a real call.
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
            # Best-effort: drain frames until we see ``session.updated``
            # (or time out). We don't strictly need the ack to keep the
            # warmup correct — the TLS + session prime is already done by
            # the time the server processes our update — but waiting for
            # it lets us close after a clean handshake instead of mid-frame.
            deadline = asyncio.get_event_loop().time() + 1.5
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except Exception:
                    break
                try:
                    data = json.loads(raw)
                except Exception:
                    continue
                if isinstance(data, dict) and data.get("type") == "session.updated":
                    break
        except Exception as exc:  # noqa: BLE001 - best-effort
            logger.debug("OpenAI Realtime warmup failed (best-effort): %s", exc)
        finally:
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass

    def _build_session_config(self) -> dict[str, Any]:
        """Build the session.update body shared by ``connect`` /
        ``open_parked_connection`` / ``warmup`` so all three paths
        prime the upstream session identically.
        """
        session_config: dict[str, Any] = {
            "input_audio_format": self.audio_format,
            "output_audio_format": self.audio_format,
            "voice": self.voice,
            "instructions": self.instructions
            or f"You are a helpful voice assistant. Respond in {self.language}. Be concise and natural.",
            # v1 turn_detection OMITS the create_response / interrupt_response
            # keys (those are GA-only on the wire). With them omitted the
            # OpenAI server applies its true-defaults (both True) — i.e. the
            # server-managed default. ``gate_response_on_transcript`` is still
            # forwarded so the v1 builder shares the GA signature, but it only
            # takes effect when ``include_response_gating`` is True.
            "turn_detection": build_turn_detection(
                self.turn_detection,
                default_type=self.vad_type,
                default_silence_ms=self.silence_duration_ms,
                include_response_gating=False,
                gate_response_on_transcript=self.gate_response_on_transcript,
            ),
            "input_audio_transcription": {
                "model": self.input_audio_transcription_model,
            },
        }
        # v1 puts noise reduction at the TOP LEVEL of session (not nested under
        # audio.input as the GA shape does). Omitted entirely when unset.
        if self.noise_reduction is not None:
            session_config["input_audio_noise_reduction"] = {
                "type": self.noise_reduction
            }
        if self.temperature is not None:
            session_config["temperature"] = self.temperature
        if self.max_response_output_tokens is not None:
            session_config["max_response_output_tokens"] = (
                self.max_response_output_tokens
            )
        if self.modalities is not None:
            session_config["modalities"] = self.modalities
        if self.tool_choice is not None:
            session_config["tool_choice"] = self.tool_choice
        if self.tools:
            session_config["tools"] = [
                self._build_tool_wire_format(t) for t in self.tools
            ]
        if self.reasoning_effort is not None:
            session_config["reasoning"] = {"effort": self.reasoning_effort}
        return session_config

    async def connect(self) -> None:
        """Connect to OpenAI Realtime API and wait for ``session.updated`` ack."""
        url = f"{self.OPENAI_REALTIME_URL}?model={self.model}"
        self._ws = await websockets.connect(
            url,
            additional_headers={
                "Authorization": f"Bearer {self.api_key}",
            },
            # Keep the connection alive on long conversational pauses; a
            # dropped WS mid-call is the single most common failure on
            # carrier links with aggressive NAT timeouts.
            ping_interval=20,
            ping_timeout=20,
        )
        self._running = True

        try:
            # Wait for session.created
            response = await asyncio.wait_for(self._ws.recv(), timeout=15.0)
            data = json.loads(response)
            if data.get("type") != "session.created":
                # Surface the actual server-side error so callers see the
                # OpenAI message (auth, model not available, quota, etc.)
                # rather than the bare "got error" wrapper.
                err = data.get("error") or {}
                msg = err.get("message") or err.get("code") or str(data)
                raise RuntimeError(
                    f"Expected session.created, got {data.get('type')!r}: {msg}"
                )

            await self._ws.send(
                json.dumps(
                    {
                        "type": "session.update",
                        "session": self._build_session_config(),
                    }
                )
            )

            # Wait for ``session.updated`` ack before allowing any audio /
            # text traffic. Without this the first turn races the config
            # and OpenAI sometimes rejects the initial audio buffer.
            await self._await_session_updated()
        except Exception:
            await self._ws.close()
            self._ws = None
            self._running = False
            raise

    async def open_parked_connection(self):  # type: ignore[no-untyped-def]
        """Open a fresh Realtime WS, exchange ``session.created`` /
        ``session.update`` / ``session.updated`` so the upstream session
        is fully primed, and return the OPEN socket WITHOUT taking it
        on ``self._ws``.

        Used by the prewarm pipeline to park a Realtime connection
        during ringing; the live consumer adopts it via
        :meth:`adopt_websocket`.

        Bounded by 8 s. Raises on timeout / handshake failure — the
        prewarm pipeline treats any error as a cache miss and the call
        falls through to the cold ``connect()`` path.

        Billing safety: ``session.update`` does not invoke the model.
        No tokens are billed.
        """
        url = f"{self.OPENAI_REALTIME_URL}?model={self.model}"
        ws = await asyncio.wait_for(
            websockets.connect(
                url,
                additional_headers={
                    "Authorization": f"Bearer {self.api_key}",
                },
                ping_interval=20,
                ping_timeout=20,
            ),
            timeout=8.0,
        )
        try:
            response = await asyncio.wait_for(ws.recv(), timeout=2.0)
            data = json.loads(response)
            if data.get("type") != "session.created":
                err = data.get("error") or {}
                msg = err.get("message") or err.get("code") or str(data)
                raise RuntimeError(
                    f"Expected session.created, got {data.get('type')!r}: {msg}"
                )
            await ws.send(
                json.dumps(
                    {
                        "type": "session.update",
                        "session": self._build_session_config(),
                    }
                )
            )
            # Drain frames until session.updated (or 1.5 s timeout).
            deadline = asyncio.get_event_loop().time() + 1.5
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except Exception:
                    break
                try:
                    data = json.loads(raw)
                except Exception:
                    continue
                if isinstance(data, dict) and data.get("type") == "session.updated":
                    break
        except Exception:
            try:
                await ws.close()
            except Exception:
                pass
            raise
        return ws

    def adopt_websocket(self, ws) -> None:  # type: ignore[no-untyped-def]
        """Adopt a pre-opened, already-``session.updated`` Realtime WS
        produced by the prewarm pipeline. Skips the fresh
        ``websockets.connect`` + ``session.created`` /
        ``session.update`` round-trip — saves ~250-450 ms on first turn.

        Caller MUST verify the WS is still alive (``not ws.closed``)
        before calling and MUST have already received
        ``session.updated`` on the parked socket. If the parked WS died
        between park and adopt, fall back to :meth:`connect`.
        """
        self._ws = ws
        self._running = True

    async def _await_session_updated(self) -> None:
        """Read a single post-``session.update`` message and return.

        Wraps one ``recv()`` call in ``asyncio.wait_for`` so the inner
        coroutine is properly cancelled under both real websocket and
        AsyncMock semantics. If the first message is not ``session.updated``
        we buffer it for the normal receive loop and return anyway — any
        subsequent audio traffic would race the ack on a real socket only in
        edge cases, which the outer timeout handler used to paper over.
        """
        try:
            raw = await asyncio.wait_for(
                self._ws.recv(), timeout=self._SESSION_UPDATE_TIMEOUT
            )
        except TimeoutError:
            logger.warning(
                "OpenAI Realtime: no message received after %.1fs while "
                "waiting for session.updated; continuing anyway",
                self._SESSION_UPDATE_TIMEOUT,
            )
            return
        try:
            data = json.loads(raw)
        except Exception:  # pragma: no cover — malformed JSON
            return
        if data.get("type") != "session.updated":
            # Buffer for the normal receive loop to drain.
            self._pending_events.append(raw)

    async def send_audio(self, audio: bytes) -> None:
        """Send audio to OpenAI Realtime API (format must match configured audio_format)."""
        if self._ws is None:
            return
        encoded = base64.b64encode(audio).decode("ascii")
        await self._ws.send(
            json.dumps(
                {
                    "type": "input_audio_buffer.append",
                    "audio": encoded,
                }
            )
        )

    async def receive_events(self):
        """Yield events from OpenAI Realtime API.

        Yields tuples of (event_type, data):
        - ("audio", bytes) — audio chunk to send to Twilio
        - ("transcript_input", str) — what the user said
        - ("transcript_output", str) — what the AI said
        - ("speech_started", None) — user started speaking (barge-in)
        - ("response_done", None) — AI finished responding
        - ("error", dict) — surfaced error from the server / transport
        """
        if self._ws is None:
            return

        async def _iter_raw():
            # Drain anything buffered during ``connect()`` first, then stream
            # from the socket. Using an inner async-gen keeps the public
            # iterator shape unchanged while making ``close()`` able to
            # cancel the read cleanly.
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

                if event_type == "response.audio.delta":
                    # Audio chunk from AI — in the configured audio_format
                    audio_bytes = base64.b64decode(data.get("delta", ""))
                    # Rough book-keeping so we can truncate on barge-in. At
                    # 8 kHz / 1 B per sample (g711) this is bytes; at 16 kHz
                    # PCM16 it's bytes/2. We only use it as a capped value
                    # passed to ``conversation.item.truncate`` so a coarse
                    # estimate is good enough — the server clamps it.
                    self._current_response_audio_ms += _estimate_audio_ms(
                        audio_bytes,
                        self.audio_format,
                    )
                    # Record wall-clock arrival of the first chunk for this
                    # response so ``cancel_response`` can bound truncate to
                    # what could plausibly have been played in real time.
                    if self._current_response_first_audio_at is None:
                        self._current_response_first_audio_at = time.monotonic()
                    yield ("audio", audio_bytes)

                elif event_type == "response.audio_transcript.delta":
                    # What the AI is saying (text)
                    yield ("transcript_output", data.get("delta", ""))

                elif event_type in (
                    "response.content_part.added",
                    "response.output_item.added",
                ):
                    # Capture the in-flight assistant item id so we can
                    # truncate it precisely on barge-in.
                    item = data.get("item") or {}
                    item_id = item.get("id") or data.get("item_id")
                    if item_id:
                        self._current_response_item_id = item_id
                        self._current_response_audio_ms = 0
                        self._current_response_first_audio_at = None

                elif event_type == "input_audio_buffer.speech_started":
                    # User started speaking — barge-in.
                    yield ("speech_started", None)

                elif event_type == "input_audio_buffer.speech_stopped":
                    yield ("speech_stopped", None)

                elif (
                    event_type
                    == "conversation.item.input_audio_transcription.completed"
                ):
                    # What the user said
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
                    # End of response — clear tracking state so the next
                    # turn starts with a fresh item id.
                    self._current_response_item_id = None
                    self._current_response_audio_ms = 0
                    self._current_response_first_audio_at = None
                    yield ("response_done", data.get("response", {}))

                elif event_type == "error":
                    err = data.get("error", {})
                    logger.error("OpenAI Realtime error: %s", err)
                    yield ("error", err)

        except websockets.exceptions.ConnectionClosed as exc:
            if self._running and getattr(exc, "code", 1000) != 1000:
                # Surface unexpected closes so the caller can decide whether
                # to reconnect. We intentionally don't reconnect here —
                # telephony carriers handle session lifecycle.
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

    async def truncate_playback(self) -> None:
        """Truncate the in-flight assistant item to what the caller heard.

        Sends ONLY ``conversation.item.truncate`` (no ``response.cancel``).
        This is the WebSocket-transport bookkeeping required on barge-in: per
        OpenAI's docs the GA server auto-truncates only on WebRTC / SIP, so a
        WebSocket client (Patter's ``wss://api.openai.com/v1/realtime``
        transport) MUST send the truncate itself so the partially-generated
        assistant message does not linger on the conversation as "ghost text"
        the model replays next turn.

        On the SERVER-MANAGED barge-in path (``interrupt_response: true``) this
        is the ONLY adapter call: the server already cancels the response, so
        sending ``response.cancel`` from the client would be redundant /
        rejected. The legacy client-managed path uses
        :meth:`cancel_response` (truncate + cancel) instead.

        ``audio_end_ms`` MUST reflect what the caller actually heard, not
        what the server generated. OpenAI streams audio at 5-10x real-time,
        so the byte-derived counter overstates playback whenever the
        consumer cleared its playout buffer (e.g. ``send_clear``) before
        the audio reached the speaker. We bound the truncate point by
        wall-clock time since the first chunk of this response — that's the
        physical maximum a 1x real-time playback could have produced.
        """
        if self._ws is None:
            return
        if not self._current_response_item_id:
            # No response in flight — nothing to truncate. Silent no-op keeps
            # the call idempotent across stale callers / phantom barge-ins.
            return
        audio_end_ms = self._current_response_audio_ms
        if self._current_response_first_audio_at is not None:
            # Cap by wall-clock playback time. Subtracting from the
            # generated total keeps audio_end_ms ≥ 0 and ≤ generated_ms.
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
        # Reset per-response tracking so subsequent audio chunks (post-truncate
        # late frames) and the next response.create start clean.
        self._current_response_item_id = None
        self._current_response_audio_ms = 0
        self._current_response_first_audio_at = None

    async def cancel_response(self) -> None:
        """Cancel current AI response AND truncate the in-flight item.

        Full client-managed barge-in: ``conversation.item.truncate`` (so the
        partially-generated assistant message does not linger as "ghost text")
        followed by ``response.cancel`` (so the server stops generating).

        Used by the LEGACY opt-out path (``gate_response_on_transcript=True``,
        ``interrupt_response: false``) and by explicit cancels (e.g. a blocked
        guardrail rewrite). On the server-managed default path the handler
        calls :meth:`truncate_playback` instead — the server owns the cancel
        via ``interrupt_response: true`` and a client ``response.cancel`` would
        be redundant.

        Idempotent: when no response is in flight :meth:`truncate_playback`
        no-ops and the ``response.cancel`` is skipped — OpenAI Realtime GA
        rejects unconditional ``response.cancel`` with
        ``response_cancel_not_active``, so guarding it here keeps phantom VAD
        ``speech_started`` events (echo of agent audio, voicemail beep, line
        noise) from spamming ERROR-level logs.
        """
        if self._ws is None:
            return
        if not self._current_response_item_id:
            # No response in flight — nothing to cancel. Mirror the old guard
            # so unconditional cancels stay no-ops (see docstring).
            return
        # Truncate first (resets the per-response tracking fields), then cancel.
        await self.truncate_playback()
        try:
            await self._ws.send(json.dumps({"type": "response.cancel"}))
        except Exception as exc:
            logger.debug("response.cancel failed: %s", exc)

    async def send_text(self, text: str) -> None:
        """Send a text message to the AI (triggers a spoken response)."""
        if self._ws is None:
            return
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

    async def request_response(self) -> None:
        """Trigger ``response.create`` with no new user item.

        Used by the Realtime stream-handler to drive a response after the
        client-side hallucination filter accepts an
        ``input_audio_transcription.completed`` event. The server VAD
        config sets ``create_response: false`` so OpenAI no longer
        auto-creates a response on every ``input_audio_buffer.committed``;
        Patter is now responsible for triggering it explicitly when a
        real user turn lands.
        """
        if self._ws is None:
            return
        await self._ws.send(json.dumps({"type": "response.create"}))

    async def send_first_message(self, text: str) -> None:
        """Make the AI speak ``text`` as its opening line.

        Triggers ``response.create`` with explicit ``instructions`` that
        force the model to render ``text`` verbatim as its first audio
        utterance. This is the correct semantics for ``Agent.first_message``
        per its docstring ("What the AI says when the callee answers").

        Without this, ``send_text(first_message)`` would inject ``text`` as
        ``role: user`` and the AI would *reply* to its own greeting,
        producing role-confused openings (e.g. a receptionist agent
        responding "I'd like to schedule a haircut" because it took its own
        first_message as a customer cue).
        """
        if self._ws is None:
            return
        await self._ws.send(
            json.dumps(
                {
                    "type": "response.create",
                    "response": {
                        "modalities": ["audio", "text"],
                        "instructions": (
                            f"Say exactly the following sentence as your first turn "
                            f'and nothing else: "{text}"'
                        ),
                    },
                }
            )
        )

    async def send_reassurance(self, text: str) -> None:
        """Speak a short reassurance filler WITHOUT adding a ``role:user`` turn.

        Same no-fake-turn shape as :meth:`send_first_message`: a bare
        ``response.create`` carrying explicit ``instructions`` so the filler is
        the assistant's own in-band audio. The reassurance scheduler in the
        stream-handler routes here instead of :meth:`send_text` (which would
        inject a phantom ``role:user`` item and corrupt the transcript).
        Fillers must not imply success or failure.

        The GA adapter overrides this with the ``output_modalities`` shape;
        this v1-beta variant keeps the ``modalities`` key its own
        :meth:`send_first_message` uses so the two no-fake-turn paths never
        drift on this endpoint.
        """
        if self._ws is None:
            return
        await self._ws.send(
            json.dumps(
                {
                    "type": "response.create",
                    "response": {
                        "modalities": ["audio", "text"],
                        "instructions": f'Say exactly this and nothing else: "{text}"',
                    },
                }
            )
        )

    async def send_function_result(self, call_id: str, result: str) -> None:
        """Send a function call result back to OpenAI and trigger a new response."""
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

    async def close(self) -> None:
        """Close the connection and cancel any in-flight receive task."""
        self.record_session_end()
        self._running = False
        task = self._receive_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            self._receive_task = None
        if self._ws:
            await self._ws.close()
            self._ws = None


def _estimate_audio_ms(chunk: bytes, audio_format: str) -> int:
    """Rough audio duration estimate used for truncation accounting.

    - ``g711_ulaw`` / ``g711_alaw``: 8 kHz, 1 byte/sample  → ms = bytes/8
    - ``pcm16``: OpenAI Realtime uses 24 kHz, 2 bytes/sample → ms = bytes/48
      (Fix 2: the API spec documents 24 kHz for pcm16, not 16 kHz.
       24000 samples/s * 2 bytes/sample / 1000 ms = 48 bytes/ms.)
    """
    if not chunk:
        return 0
    if audio_format in (
        OpenAIRealtimeAudioFormat.G711_ULAW.value,
        OpenAIRealtimeAudioFormat.G711_ALAW.value,
    ):
        return len(chunk) // 8
    if audio_format == OpenAIRealtimeAudioFormat.PCM16.value:
        # 24 kHz × 2 bytes/sample = 48 bytes per millisecond
        return len(chunk) // 48
    return 0
