"""Gemini Live realtime adapter.

Partially adapted (~65% port) from LiveKit Agents
(livekit-plugins/livekit-plugins-google/livekit/plugins/google/realtime/realtime_api.py,
Apache 2.0) at commit referenced in the PR body. Reframed to Patter's simpler
realtime adapter surface (``connect``/``send_audio``/``receive_events``/``close``)
which mirrors :class:`~getpatter.providers.openai_realtime.OpenAIRealtimeAdapter`.

The heavy LiveKit lifecycle/resume machinery is intentionally NOT ported —
Patter's handlers manage session lifecycle externally.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any, AsyncIterator

logger = logging.getLogger("patter.gemini_live")

# Default PCM audio format used on the wire for Gemini Live.
# Gemini Live requires PCM16 mono; sample-rate negotiation happens via
# ``speech_config``. Patter callers should resample to 16 kHz before calling
# :meth:`GeminiLiveAdapter.send_audio`.
DEFAULT_INPUT_SAMPLE_RATE_HZ = 16000
DEFAULT_OUTPUT_SAMPLE_RATE_HZ = 24000


class GeminiLiveAdapter:
    """Bridges a bidirectional audio stream to Google Gemini Live.

    The adapter presents the same surface as :class:`OpenAIRealtimeAdapter`:
    ``connect() -> send_audio(bytes) -> async for event in receive_events() -> close()``.

    Tool calling is supported via the ``tools`` constructor argument (same
    shape as the OpenAI adapter); function-call events are emitted as
    ``("function_call", {"call_id", "name", "arguments"})``.

    Requires ``google-genai>=1.55`` installed (``pip install getpatter[gemini-live]``).
    The SDK is imported lazily so callers that do not use Gemini Live do not
    pay the import cost.
    """

    def __init__(
        self,
        api_key: str,
        # gemini-2.0-flash-exp was experimental preview retired from docs.
        # Current Live API model: gemini-live-2.5-flash-preview. Override via
        # GeminiLive(model=...) if you need another variant.
        model: str = "gemini-live-2.5-flash-preview",
        voice: str = "Puck",
        instructions: str = "",
        language: str = "en-US",
        tools: list[dict] | None = None,
        input_sample_rate: int = DEFAULT_INPUT_SAMPLE_RATE_HZ,
        output_sample_rate: int = DEFAULT_OUTPUT_SAMPLE_RATE_HZ,
        temperature: float = 0.8,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.voice = voice
        self.instructions = instructions
        self.language = language
        self.tools = tools
        self.input_sample_rate = input_sample_rate
        self.output_sample_rate = output_sample_rate
        self.temperature = temperature
        self._client: Any = None
        self._session: Any = None
        self._session_cm: Any = None
        self._running = False

    def __repr__(self) -> str:
        return (
            f"GeminiLiveAdapter(model={self.model!r}, voice={self.voice!r}, "
            f"language={self.language!r})"
        )

    async def connect(self) -> None:
        """Open a Live session with Gemini.

        Lazily imports ``google.genai`` — raises ``RuntimeError`` with a
        helpful install hint if the extra is missing.
        """
        try:
            from google import genai  # type: ignore[import-not-found]
            from google.genai import types as genai_types  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - exercised in production
            raise RuntimeError(
                "Gemini Live requires the 'google-genai' package. "
                "Install with: pip install getpatter[gemini-live]"
            ) from exc

        self._client = genai.Client(
            api_key=self.api_key,
            http_options={"api_version": "v1alpha"},
        )

        speech_config = genai_types.SpeechConfig(
            voice_config=genai_types.VoiceConfig(
                prebuilt_voice_config=genai_types.PrebuiltVoiceConfig(
                    voice_name=self.voice,
                ),
            ),
            language_code=self.language,
        )

        config: dict[str, Any] = {
            "response_modalities": ["AUDIO"],
            "speech_config": speech_config,
            "temperature": self.temperature,
        }
        if self.instructions:
            config["system_instruction"] = genai_types.Content(
                parts=[genai_types.Part(text=self.instructions)],
            )
        if self.tools:
            config["tools"] = [
                {
                    "function_declarations": [
                        {
                            "name": t["name"],
                            "description": t.get("description", ""),
                            "parameters": t.get("parameters", {}),
                        }
                        for t in self.tools
                    ],
                }
            ]

        # ``aio.live.connect`` returns an async context manager — we enter it
        # manually so that ``receive_events`` can stream indefinitely.
        self._session_cm = self._client.aio.live.connect(
            model=self.model,
            config=config,
        )
        self._session = await self._session_cm.__aenter__()
        self._running = True

    async def send_audio(self, audio: bytes) -> None:
        """Send a PCM16 mono chunk at ``input_sample_rate`` Hz."""
        if self._session is None:
            return
        mime_type = f"audio/pcm;rate={self.input_sample_rate}"
        await self._session.send_realtime_input(
            media={"data": audio, "mime_type": mime_type},
        )

    async def send_text(self, text: str) -> None:
        """Send a user text turn that triggers a spoken response."""
        if self._session is None:
            return
        await self._session.send_client_content(
            turns={"role": "user", "parts": [{"text": text}]},
            turn_complete=True,
        )

    async def send_function_result(self, call_id: str, result: str) -> None:
        """Return a tool call result to Gemini and continue the turn."""
        if self._session is None:
            return
        await self._session.send_tool_response(
            function_responses=[
                {
                    "id": call_id,
                    "name": call_id,  # LiveKit also fans out id as name fallback
                    "response": {"result": result},
                }
            ],
        )

    async def cancel_response(self) -> None:
        """Interrupt the current model turn (barge-in)."""
        # Gemini Live's ``send_realtime_input`` implicitly barges on VAD;
        # explicit cancel is not part of the v1alpha wire protocol.
        logger.debug("Gemini Live: cancel_response is implicit via VAD")

    async def receive_events(self) -> AsyncIterator[tuple[str, Any]]:
        """Yield ``(event_type, payload)`` tuples.

        Event types:
            ``audio`` — ``bytes`` of PCM16 mono audio at ``output_sample_rate``
            ``transcript_output`` — partial transcript text (if enabled)
            ``function_call`` — ``{"call_id", "name", "arguments"}``
            ``response_done`` — server indicated turn completion
        """
        if self._session is None:
            return

        try:
            async for response in self._session.receive():
                # ``response`` is a genai ``LiveServerMessage``. We gracefully
                # introspect it so the adapter stays stable across minor
                # google-genai releases.
                server_content = getattr(response, "server_content", None)
                if server_content is not None:
                    model_turn = getattr(server_content, "model_turn", None)
                    if model_turn is not None:
                        for part in getattr(model_turn, "parts", []) or []:
                            inline = getattr(part, "inline_data", None)
                            if inline is not None and getattr(inline, "data", None):
                                yield ("audio", inline.data)
                            text = getattr(part, "text", None)
                            if text:
                                yield ("transcript_output", text)
                    if getattr(server_content, "turn_complete", False):
                        yield ("response_done", None)
                    if getattr(server_content, "interrupted", False):
                        yield ("speech_started", None)

                tool_call = getattr(response, "tool_call", None)
                if tool_call is not None:
                    for fn in getattr(tool_call, "function_calls", []) or []:
                        args = getattr(fn, "args", {}) or {}
                        yield (
                            "function_call",
                            {
                                "call_id": getattr(fn, "id", "") or "",
                                "name": getattr(fn, "name", "") or "",
                                "arguments": json.dumps(args)
                                if not isinstance(args, str)
                                else args,
                            },
                        )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("Gemini Live receive error: %s", exc)
        finally:
            self._running = False

    async def close(self) -> None:
        """Tear down the Live session."""
        self._running = False
        if self._session_cm is not None:
            try:
                await self._session_cm.__aexit__(None, None, None)
            except Exception as exc:  # pragma: no cover
                logger.debug("Gemini Live close error: %s", exc)
            finally:
                self._session_cm = None
                self._session = None
        self._client = None
