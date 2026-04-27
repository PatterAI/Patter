import asyncio
import base64
import json
import logging
from typing import Any

import httpx
import websockets

logger = logging.getLogger("getpatter")

ELEVENLABS_CONVAI_URL = "wss://api.elevenlabs.io/v1/convai/conversation"
ELEVENLABS_SIGNED_URL = (
    "https://api.elevenlabs.io/v1/convai/conversation/get-signed-url"
)

# Silence threshold: if no audio chunk arrives for this many ms after
# `agent_response`, treat the agent turn as finished and emit `response_done`.
_AGENT_SILENCE_MS = 500


class ElevenLabsConvAIAdapter:
    """Bridges Twilio/Telnyx media stream to ElevenLabs Conversational AI.

    Handles full conversation: STT + LLM + TTS in one WebSocket.
    Uses ElevenLabs premium voices.
    """

    def __init__(
        self,
        api_key: str,
        agent_id: str,
        voice_id: str = "EXAVITQu4vr4xnSDxMaL",
        model_id: str = "eleven_flash_v2_5",
        language: str = "it",
        first_message: str = "",
        output_audio_format: str | None = None,
        input_audio_format: str | None = None,
        use_signed_url: bool = False,
    ):
        if not agent_id:
            raise ValueError(
                "ElevenLabsConvAIAdapter requires a non-empty agent_id. "
                "Create an agent in the ElevenLabs Conversational AI dashboard "
                "and pass its id."
            )
        self.api_key = api_key
        self.agent_id = agent_id
        self.voice_id = voice_id
        self.model_id = model_id
        self.language = language
        self.first_message = first_message
        self.output_audio_format = output_audio_format
        self.input_audio_format = input_audio_format
        self.use_signed_url = use_signed_url
        # Populated from `conversation_initiation_metadata`.
        self.conversation_id: str | None = None
        self.agent_output_audio_format: str | None = None
        self.user_input_audio_format: str | None = None
        self._ws = None
        self._running = False
        # Async queue bridging the message-reader task to `receive_events`.
        self._events: asyncio.Queue[tuple[str, Any] | None] | None = None
        self._reader_task: asyncio.Task | None = None
        # Silence-tracking for synthetic `response_done` emission.
        self._silence_task: asyncio.Task | None = None
        self._agent_speaking = False

    def __repr__(self) -> str:
        return f"ElevenLabsConvAIAdapter(agent_id={self.agent_id!r}, model_id={self.model_id!r})"

    async def _fetch_signed_url(self) -> str:
        """Fetch a short-lived signed WS URL so we don't have to send the API key."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                ELEVENLABS_SIGNED_URL,
                params={"agent_id": self.agent_id},
                headers={"xi-api-key": self.api_key},
            )
            resp.raise_for_status()
            data = resp.json()
            signed = data.get("signed_url")
            if not signed:
                raise RuntimeError(
                    "ElevenLabs signed-url response missing 'signed_url' field"
                )
            return signed

    async def connect(self) -> None:
        """Connect to ElevenLabs Conversational AI."""
        if self.use_signed_url:
            if not self.agent_id:
                raise ValueError("use_signed_url=True requires agent_id")
            url = await self._fetch_signed_url()
            # Signed URL embeds auth — no xi-api-key header needed.
            self._ws = await websockets.connect(url)
        else:
            url = ELEVENLABS_CONVAI_URL
            if self.agent_id:
                url = f"{url}?agent_id={self.agent_id}"
            self._ws = await websockets.connect(
                url,
                additional_headers={"xi-api-key": self.api_key},
            )

        self._running = True

        # Build conversation_config_override with all optional fields plumbed.
        agent_cfg: dict[str, Any] = {}
        if self.first_message:
            agent_cfg["first_message"] = self.first_message
        if self.language:
            agent_cfg["language"] = self.language

        override: dict[str, Any] = {"tts": {"voice_id": self.voice_id}}
        if self.output_audio_format:
            override["tts"]["output_format"] = self.output_audio_format
        if self.input_audio_format:
            override["asr"] = {"input_format": self.input_audio_format}
        if agent_cfg:
            override["agent"] = agent_cfg

        config: dict[str, Any] = {
            "type": "conversation_initiation_client_data",
            "conversation_config_override": override,
        }

        await self._ws.send(json.dumps(config))

        # Kick off the background reader that drains the socket and feeds the
        # async-iterator queue exposed via `receive_events`.
        self._events = asyncio.Queue()
        self._reader_task = asyncio.create_task(self._read_loop())

    async def send_audio(self, audio_bytes: bytes) -> None:
        """Send user audio to ElevenLabs.

        Per the ConvAI protocol, inbound caller audio is sent as a JSON
        message with a top-level `user_audio_chunk` key holding base64-encoded
        PCM (format must match the agent's `user_input_audio_format`).
        """
        if self._ws is None:
            return
        await self._ws.send(
            json.dumps(
                {"user_audio_chunk": base64.b64encode(audio_bytes).decode("ascii")}
            )
        )

    async def _respond_to_ping(self, event_id: Any, delay_ms: int) -> None:
        """Reply to a `ping` event with the matching `pong`, optionally after ping_ms."""
        try:
            if delay_ms and delay_ms > 0:
                await asyncio.sleep(delay_ms / 1000.0)
            if self._ws is None:
                return
            await self._ws.send(json.dumps({"type": "pong", "event_id": event_id}))
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("ElevenLabs ConvAI pong send failed: %s", exc)

    async def _emit_response_done_after_silence(self) -> None:
        """Emit `response_done` after `_AGENT_SILENCE_MS` without audio."""
        try:
            await asyncio.sleep(_AGENT_SILENCE_MS / 1000.0)
            if self._agent_speaking and self._events is not None:
                self._agent_speaking = False
                await self._events.put(("response_done", {}))
        except asyncio.CancelledError:
            pass

    def _reset_silence_timer(self) -> None:
        """Cancel any pending silence timer (called on every agent-audio chunk)."""
        if self._silence_task and not self._silence_task.done():
            self._silence_task.cancel()
        self._silence_task = None

    async def _finalize_agent_turn(self) -> None:
        """If an agent turn is in progress, emit `response_done` now."""
        self._reset_silence_timer()
        if self._agent_speaking and self._events is not None:
            self._agent_speaking = False
            await self._events.put(("response_done", {}))

    async def _read_loop(self) -> None:
        """Drain the WS and push parsed events onto the async queue."""
        assert self._events is not None
        try:
            async for raw in self._ws:
                try:
                    data = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                msg_type = data.get("type", "")

                if msg_type == "ping":
                    # Server expects a pong within ~20 s or it terminates the WS.
                    ping_payload = data.get("ping_event") or data.get("ping") or {}
                    event_id = ping_payload.get("event_id") or data.get("event_id")
                    ping_ms = ping_payload.get("ping_ms") or 0
                    asyncio.create_task(self._respond_to_ping(event_id, ping_ms))
                    continue

                if msg_type == "conversation_initiation_metadata":
                    meta = data.get("conversation_initiation_metadata_event") or data
                    self.conversation_id = meta.get("conversation_id") or self.conversation_id
                    self.agent_output_audio_format = (
                        meta.get("agent_output_audio_format")
                        or self.agent_output_audio_format
                    )
                    self.user_input_audio_format = (
                        meta.get("user_input_audio_format")
                        or self.user_input_audio_format
                    )
                    # A new turn boundary: close any dangling agent turn.
                    await self._finalize_agent_turn()
                    continue

                if msg_type == "audio":
                    # Audio payload may be nested under `audio_event` per current
                    # ElevenLabs schema, or flat — support both.
                    audio_b64 = ""
                    audio_evt = data.get("audio_event")
                    if isinstance(audio_evt, dict):
                        audio_b64 = audio_evt.get("audio_base_64") or audio_evt.get(
                            "audio", ""
                        )
                    if not audio_b64:
                        audio_b64 = data.get("audio", "")
                    if audio_b64:
                        # Reset silence timer on every agent audio chunk.
                        self._reset_silence_timer()
                        self._agent_speaking = True
                        await self._events.put(
                            ("audio", base64.b64decode(audio_b64))
                        )
                        # Schedule silence-based response_done.
                        self._silence_task = asyncio.create_task(
                            self._emit_response_done_after_silence()
                        )
                    continue

                if msg_type == "user_transcript":
                    evt = data.get("user_transcription_event") or data
                    text = evt.get("user_transcript") or evt.get("text", "")
                    # User speaking boundary: close any pending agent turn.
                    await self._finalize_agent_turn()
                    await self._events.put(("transcript_input", text))
                    continue

                if msg_type == "agent_response":
                    evt = data.get("agent_response_event") or data
                    text = evt.get("agent_response") or evt.get("text", "")
                    await self._events.put(("transcript_output", text))
                    # `agent_response` marks the START of the agent turn — do
                    # NOT emit response_done here. It will be emitted by the
                    # silence watcher or on interruption / next turn.
                    self._agent_speaking = True
                    await self._events.put(("response_start", {"text": text}))
                    continue

                if msg_type == "interruption":
                    await self._finalize_agent_turn()
                    await self._events.put(("interruption", None))
                    continue

                if msg_type == "error":
                    err_text = (
                        data.get("message")
                        or data.get("error")
                        or json.dumps(data)
                    )
                    logger.error("ElevenLabs ConvAI error: %s", err_text)
                    await self._events.put(("error", err_text))
                    continue

        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as exc:  # pragma: no cover — defensive
            if self._events is not None:
                await self._events.put(("error", f"read_loop: {exc}"))
        finally:
            self._running = False
            if self._events is not None:
                await self._events.put(None)

    async def receive_events(self):
        """Yield events from ElevenLabs ConvAI.

        Yields tuples of (event_type, data):
        - ("audio", bytes) — agent audio chunk to forward to the caller
        - ("transcript_input", str) — what the user said
        - ("transcript_output", str) — what the AI said (full turn text)
        - ("response_start", dict) — agent begins speaking (start of turn)
        - ("response_done", dict) — agent turn finished (silence or interrupt)
        - ("interruption", None) — user interrupted the agent
        - ("error", str) — server-side error text
        """
        if self._events is None:
            return
        while True:
            evt = await self._events.get()
            if evt is None:
                return
            yield evt

    async def close(self) -> None:
        """Close the connection and cancel the background reader."""
        self._running = False
        self._reset_silence_timer()
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
        self._reader_task = None
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
