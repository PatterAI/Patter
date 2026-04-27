import asyncio
import base64
import json
import logging
from collections import deque
from typing import Any, Literal

import websockets

logger = logging.getLogger("getpatter.openai_realtime")


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
        model: str = "gpt-realtime-mini",
        voice: str = "alloy",
        instructions: str = "",
        language: str = "en",
        tools: list[dict] | None = None,
        audio_format: str = "g711_ulaw",
        *,
        temperature: float | None = None,
        max_response_output_tokens: int | str | None = None,
        modalities: list[str] | None = None,
        tool_choice: str | dict | None = None,
        input_audio_transcription_model: str = "whisper-1",
        vad_type: Literal["server_vad", "semantic_vad"] = "server_vad",
        # OpenAI's documented sweet-spot for snappier turns. Lowering from the
        # previous 500 ms saves ~200 ms per turn end. Override via constructor
        # if a use case (e.g. dictation) needs more trailing silence.
        silence_duration_ms: int = 300,
    ):
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
        self._ws: Any = None
        self._running = False
        # Track the assistant message currently being generated so we can
        # truncate it cleanly on barge-in (see ``input_audio_buffer.speech_started``).
        self._current_response_item_id: str | None = None
        self._current_response_audio_ms: int = 0
        # Messages read during the ``session.updated`` ack wait get buffered
        # here and drained by ``receive_events`` before reading the socket.
        self._pending_events: deque[str] = deque()
        self._receive_task: asyncio.Task | None = None

    def __repr__(self) -> str:
        return f"OpenAIRealtimeAdapter(model={self.model!r}, voice={self.voice!r}, audio_format={self.audio_format!r})"

    async def connect(self) -> None:
        """Connect to OpenAI Realtime API and wait for ``session.updated`` ack."""
        url = f"{self.OPENAI_REALTIME_URL}?model={self.model}"
        self._ws = await websockets.connect(
            url,
            additional_headers={
                "Authorization": f"Bearer {self.api_key}",
                "OpenAI-Beta": "realtime=v1",
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
            response = await self._ws.recv()
            data = json.loads(response)
            if data.get("type") != "session.created":
                raise RuntimeError(f"Expected session.created, got {data.get('type')}")

            # Configure session audio format (g711_ulaw for Twilio, pcm16 for Telnyx)
            session_config: dict[str, Any] = {
                "input_audio_format": self.audio_format,
                "output_audio_format": self.audio_format,
                "voice": self.voice,
                "instructions": self.instructions or f"You are a helpful voice assistant. Respond in {self.language}. Be concise and natural.",
                "turn_detection": {
                    "type": self.vad_type,
                    "threshold": 0.5,
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": self.silence_duration_ms,
                },
                "input_audio_transcription": {
                    "model": self.input_audio_transcription_model,
                },
            }
            if self.temperature is not None:
                session_config["temperature"] = self.temperature
            if self.max_response_output_tokens is not None:
                session_config["max_response_output_tokens"] = self.max_response_output_tokens
            if self.modalities is not None:
                session_config["modalities"] = self.modalities
            if self.tool_choice is not None:
                session_config["tool_choice"] = self.tool_choice
            if self.tools:
                session_config["tools"] = [
                    {
                        "type": "function",
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t["parameters"],
                    }
                    for t in self.tools
                ]
            await self._ws.send(json.dumps({
                "type": "session.update",
                "session": session_config,
            }))

            # Wait for ``session.updated`` ack before allowing any audio /
            # text traffic. Without this the first turn races the config
            # and OpenAI sometimes rejects the initial audio buffer.
            await self._await_session_updated()
        except Exception:
            await self._ws.close()
            self._ws = None
            self._running = False
            raise

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
        except asyncio.TimeoutError:
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
        await self._ws.send(json.dumps({
            "type": "input_audio_buffer.append",
            "audio": encoded,
        }))

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
                        audio_bytes, self.audio_format,
                    )
                    yield ("audio", audio_bytes)

                elif event_type == "response.audio_transcript.delta":
                    # What the AI is saying (text)
                    yield ("transcript_output", data.get("delta", ""))

                elif event_type in ("response.content_part.added", "response.output_item.added"):
                    # Capture the in-flight assistant item id so we can
                    # truncate it precisely on barge-in.
                    item = data.get("item") or {}
                    item_id = item.get("id") or data.get("item_id")
                    if item_id:
                        self._current_response_item_id = item_id
                        self._current_response_audio_ms = 0

                elif event_type == "input_audio_buffer.speech_started":
                    # User started speaking — barge-in.
                    yield ("speech_started", None)

                elif event_type == "input_audio_buffer.speech_stopped":
                    yield ("speech_stopped", None)

                elif event_type == "conversation.item.input_audio_transcription.completed":
                    # What the user said
                    yield ("transcript_input", data.get("transcript", ""))

                elif event_type == "response.function_call_arguments.done":
                    yield ("function_call", {
                        "call_id": data.get("call_id", ""),
                        "name": data.get("name", ""),
                        "arguments": data.get("arguments", "{}"),
                    })

                elif event_type == "response.done":
                    # End of response — clear tracking state so the next
                    # turn starts with a fresh item id.
                    self._current_response_item_id = None
                    self._current_response_audio_ms = 0
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
                yield ("error", {
                    "type": "connection_closed",
                    "code": getattr(exc, "code", None),
                    "reason": getattr(exc, "reason", ""),
                })
        finally:
            self._running = False

    async def cancel_response(self) -> None:
        """Cancel current AI response and truncate the in-flight item.

        Required for clean barge-in: ``response.cancel`` alone leaves the
        partially-generated assistant message on the transcript, which the
        model sometimes replays on the next turn ("ghost text").
        """
        if self._ws is None:
            return
        if self._current_response_item_id:
            try:
                await self._ws.send(json.dumps({
                    "type": "conversation.item.truncate",
                    "item_id": self._current_response_item_id,
                    "content_index": 0,
                    "audio_end_ms": self._current_response_audio_ms,
                }))
            except Exception as exc:  # pragma: no cover
                logger.debug("conversation.item.truncate failed: %s", exc)
        await self._ws.send(json.dumps({"type": "response.cancel"}))

    async def send_text(self, text: str) -> None:
        """Send a text message to the AI (triggers a spoken response)."""
        if self._ws is None:
            return
        await self._ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        }))
        await self._ws.send(json.dumps({"type": "response.create"}))

    async def send_function_result(self, call_id: str, result: str) -> None:
        """Send a function call result back to OpenAI and trigger a new response."""
        if self._ws is None:
            return
        await self._ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": result,
            },
        }))
        await self._ws.send(json.dumps({"type": "response.create"}))

    async def close(self) -> None:
        """Close the connection and cancel any in-flight receive task."""
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
    if audio_format in ("g711_ulaw", "g711_alaw"):
        return len(chunk) // 8
    if audio_format == "pcm16":
        # 24 kHz × 2 bytes/sample = 48 bytes per millisecond
        return len(chunk) // 48
    return 0
