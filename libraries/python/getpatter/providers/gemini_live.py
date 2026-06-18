"""Gemini Live realtime adapter.

Patter's adapter surface — ``connect`` / ``send_audio`` / ``receive_events`` /
``close`` — matches :class:`~getpatter.providers.openai_realtime.OpenAIRealtimeAdapter`,
so callers can swap providers without touching the handler. Session lifecycle
(reconnects, resumes) is managed by Patter's handlers, not by the adapter.

API version is auto-detected from the model name (see :meth:`__init__`): legacy
native-audio Gemini Live models require ``v1alpha`` and are detected by the
``native-audio`` substring, while ``gemini-3.1-flash-live-preview`` and newer
non-native-audio models use the SDK default ``v1beta``. Pass ``api_version=``
explicitly to override the auto-detection.
See: https://ai.google.dev/gemini-api/docs/live

Audio codec transcoding (mirrors :class:`OpenAIRealtime2Adapter`): the carrier
always speaks mu-law 8 kHz, so :meth:`send_audio` decodes carrier mu-law to
PCM16 and upsamples to ``input_sample_rate`` before sending, and
:meth:`receive_events` resamples Gemini's PCM output down to mu-law 8 kHz split
into 20 ms / 160-byte frames before yielding the ``audio`` event.
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
from enum import IntEnum, StrEnum
from typing import Any, AsyncIterator, Union

from getpatter.audio.transcoding import (
    StatefulResampler,
    mulaw_to_pcm16,
    pcm16_to_mulaw,
)

logger = logging.getLogger("getpatter.gemini_live")


class GeminiLiveModel(StrEnum):
    """Known Gemini Live (v1alpha) realtime models."""

    NATIVE_AUDIO_PREVIEW_09_2025 = "gemini-2.5-flash-native-audio-preview-09-2025"
    LIVE_2_5_FLASH_PREVIEW = "gemini-live-2.5-flash-preview"
    LIVE_2_0_FLASH_EXP = "gemini-2.0-flash-exp"
    FLASH_3_1_LIVE_PREVIEW = "gemini-3.1-flash-live-preview"


class GeminiLiveVoice(StrEnum):
    """Built-in voices accepted by Gemini Live ``PrebuiltVoiceConfig``."""

    PUCK = "Puck"
    CHARON = "Charon"
    KORE = "Kore"
    FENRIR = "Fenrir"
    AOEDE = "Aoede"


class GeminiLiveResponseModality(StrEnum):
    """Response modalities accepted by Gemini Live."""

    AUDIO = "AUDIO"
    TEXT = "TEXT"


class GeminiLiveApiVersion(StrEnum):
    """Gemini API versions."""

    V1ALPHA = "v1alpha"
    V1BETA = "v1beta"
    V1 = "v1"


class GeminiLiveSampleRate(IntEnum):
    """Sample rates Gemini Live accepts on the input/output streams."""

    HZ_16000 = 16000
    HZ_24000 = 24000


class GeminiLiveEventType(StrEnum):
    """Adapter-level event-type strings yielded by :meth:`receive_events`."""

    AUDIO = "audio"
    TRANSCRIPT_INPUT = "transcript_input"
    TRANSCRIPT_OUTPUT = "transcript_output"
    FUNCTION_CALL = "function_call"
    RESPONSE_DONE = "response_done"
    SPEECH_STARTED = "speech_started"


# Default PCM audio format used on the wire for Gemini Live.
# Gemini Live requires PCM16 mono; sample-rate negotiation happens via
# ``speech_config``. The adapter transcodes carrier mu-law 8 kHz to/from these
# rates internally (see :meth:`GeminiLiveAdapter.send_audio` /
# :meth:`GeminiLiveAdapter.receive_events`).
DEFAULT_INPUT_SAMPLE_RATE_HZ = GeminiLiveSampleRate.HZ_16000.value
DEFAULT_OUTPUT_SAMPLE_RATE_HZ = GeminiLiveSampleRate.HZ_24000.value

#: Convenience alias for the default Gemini 3.1 Live model.
GEMINI_LIVE_3_1_FLASH_PREVIEW: str = GeminiLiveModel.FLASH_3_1_LIVE_PREVIEW.value

# Telephony carrier audio is always mu-law (G.711) at 8 kHz. Both transcode
# directions pivot through this rate.
CARRIER_SAMPLE_RATE = 8000

# 20 ms of mu-law at 8 kHz = 160 bytes. Splitting outbound audio into 160-byte
# frames gives the StreamHandler → bridge.send_audio chain the cadence Twilio's
# playout scheduler expects; one big frame stalls playout (mirrors
# ``OpenAIRealtime2Adapter._MULAW_FRAME_BYTES``).
MULAW_FRAME_BYTES = 160

# Gain boost applied to inbound telephony audio before upsampling. Gemini's VAD,
# like the OpenAI GA server VAD, is calibrated against studio-quality audio;
# telephony-band mu-law typically sits at ~-12 dB peak relative to that. 2x gain
# lifts the signal into the VAD's expected range. Tunable for the live test.
INBOUND_GAIN = 2


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
        # gemini-2.0-flash-exp was experimental preview retired Dec 2024.
        # gemini-live-2.5-flash-preview was shut down Dec 9, 2025.
        # Current native-audio live model (v1alpha only) is the dated preview.
        # Override via GeminiLive(model=...) if needed.
        # TODO verify against Google docs: https://ai.google.dev/gemini-api/docs/live
        model: Union[
            GeminiLiveModel, str
        ] = GeminiLiveModel.NATIVE_AUDIO_PREVIEW_09_2025,
        voice: Union[GeminiLiveVoice, str] = GeminiLiveVoice.PUCK,
        instructions: str = "",
        language: str = "en-US",
        tools: list[dict] | None = None,
        input_sample_rate: Union[
            GeminiLiveSampleRate, int
        ] = GeminiLiveSampleRate.HZ_16000,
        output_sample_rate: Union[
            GeminiLiveSampleRate, int
        ] = GeminiLiveSampleRate.HZ_24000,
        temperature: float = 0.8,
        api_version: str | None = None,
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
        # Auto-detect API version: legacy native-audio models need v1alpha;
        # gemini-3.1-flash-live-preview and newer use the SDK default (v1beta).
        self._api_version: str = api_version or (
            GeminiLiveApiVersion.V1ALPHA.value
            if "native-audio" in str(model)
            else GeminiLiveApiVersion.V1BETA.value
        )
        self._client: Any = None
        self._session: Any = None
        self._session_cm: Any = None
        self._running = False
        # Tracks call_id -> function name so tool responses can be sent back
        # with the correct ``name`` field (Gemini expects the original function
        # name, not the call_id).
        self._pending_tool_calls: dict[str, str] = {}
        # SEPARATE resampler instances per direction, created lazily per
        # session. Sharing a single ``StatefulResampler`` across inbound and
        # outbound corrupts both: ``audioop.ratecv`` carries filter state, so
        # interleaving 8k→16k and 24k→8k frames through one instance produces
        # garbled audio in both directions (matches the OpenAI adapter's design
        # of distinct inbound/outbound resamplers).
        self._inbound_resampler: StatefulResampler | None = None
        self._outbound_resampler_24to16: StatefulResampler | None = None
        self._outbound_resampler_16to8: StatefulResampler | None = None

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
            http_options={"api_version": self._api_version},
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
            "response_modalities": [GeminiLiveResponseModality.AUDIO.value],
            "speech_config": speech_config,
            "temperature": self.temperature,
            # Without these, native-audio sessions produced NO user
            # transcript ever and no assistant transcript in AUDIO modality —
            # logs/history/metrics got nothing for Gemini Live calls.
            "input_audio_transcription": {},
            "output_audio_transcription": {},
        }
        if self.instructions:
            config["system_instruction"] = genai_types.Content(
                parts=[genai_types.Part(text=self.instructions)],
            )
        if self.tools:
            from getpatter.providers.google_llm import _sanitize_gemini_schema

            config["tools"] = [
                {
                    "function_declarations": [
                        {
                            "name": t["name"],
                            "description": t.get("description", ""),
                            # Strip JSON-Schema keys the Live API's proto
                            # Schema rejects ($schema, additionalProperties —
                            # emitted by strict-mode and zod-derived MCP
                            # tools): one such tool 400'd the whole session.
                            "parameters": _sanitize_gemini_schema(
                                t.get("parameters", {})
                            ),
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

    def _transcode_inbound_mulaw8_to_pcm(self, mulaw8: bytes) -> bytes:
        """Carrier mu-law 8 kHz → PCM16 mono at ``input_sample_rate``.

        Decodes mu-law to PCM16 @ 8 kHz, applies the named :data:`INBOUND_GAIN`
        boost (clamped to Int16 to avoid wrap-around), then upsamples
        8 kHz → ``input_sample_rate`` via the dedicated inbound resampler. When
        ``input_sample_rate == 8000`` the upsample is skipped.
        """
        pcm8 = mulaw_to_pcm16(mulaw8)
        num_samples = len(pcm8) // 2
        if num_samples == 0:
            return b""
        boosted = bytearray(num_samples * 2)
        for i in range(num_samples):
            sample = struct.unpack_from("<h", pcm8, i * 2)[0] * INBOUND_GAIN
            struct.pack_into("<h", boosted, i * 2, max(-32768, min(32767, sample)))
        if int(self.input_sample_rate) == CARRIER_SAMPLE_RATE:
            return bytes(boosted)
        if self._inbound_resampler is None:
            self._inbound_resampler = StatefulResampler(
                src_rate=CARRIER_SAMPLE_RATE, dst_rate=int(self.input_sample_rate)
            )
        return self._inbound_resampler.process(bytes(boosted))

    async def send_audio(self, audio: bytes) -> None:
        """Send a carrier mu-law 8 kHz chunk, transcoded to PCM for Gemini.

        Mirrors :class:`OpenAIRealtime2Adapter`: the inbound telephony audio is
        mu-law 8 kHz, so it is decoded + gain-boosted + upsampled to
        ``input_sample_rate`` before being sent in the ``audio`` field.
        """
        if self._session is None:
            return
        pcm = self._transcode_inbound_mulaw8_to_pcm(audio)
        if not pcm:
            return  # resampler warmup — no whole frame yet
        mime_type = f"audio/pcm;rate={self.input_sample_rate}"
        await self._session.send_realtime_input(
            audio={"data": pcm, "mime_type": mime_type},
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
        # Gemini requires the original function name in the response, not the
        # call_id. Look it up from the map populated when the tool call was
        # emitted; fall back to ``call_id`` if we never saw this id (defensive).
        name = self._pending_tool_calls.pop(call_id, call_id)
        await self._session.send_tool_response(
            function_responses=[
                {
                    "id": call_id,
                    "name": name,
                    "response": {"result": result},
                }
            ],
        )

    async def cancel_response(self) -> None:
        """Interrupt the current model turn (barge-in)."""
        # Gemini Live's ``send_realtime_input`` implicitly barges on VAD;
        # explicit cancel is not part of the v1alpha wire protocol.
        logger.debug("Gemini Live: cancel_response is implicit via VAD")

    def _transcode_outbound_pcm_to_mulaw8(self, pcm: bytes) -> bytes:
        """Gemini PCM16 @ ``output_sample_rate`` → carrier mu-law 8 kHz.

        Mirrors :class:`OpenAIRealtime2Adapter`: resamples
        ``output_sample_rate`` → 16 kHz → 8 kHz via a SEPARATE pair of
        resamplers (the 16k→8k stage applies audioop's anti-alias FIR, avoiding
        raspy speech), then mu-law-encodes. When ``output_sample_rate == 8000``
        only the mu-law encode runs.
        """
        if not pcm:
            return b""
        if int(self.output_sample_rate) == CARRIER_SAMPLE_RATE:
            return pcm16_to_mulaw(pcm)
        if self._outbound_resampler_24to16 is None:
            self._outbound_resampler_24to16 = StatefulResampler(
                src_rate=int(self.output_sample_rate), dst_rate=16000
            )
            self._outbound_resampler_16to8 = StatefulResampler(
                src_rate=16000, dst_rate=CARRIER_SAMPLE_RATE
            )
        pcm16 = self._outbound_resampler_24to16.process(pcm)
        pcm8 = self._outbound_resampler_16to8.process(pcm16)  # type: ignore[union-attr]
        if not pcm8:
            return b""
        return pcm16_to_mulaw(pcm8)

    async def receive_events(self) -> AsyncIterator[tuple[str, Any]]:
        """Yield ``(event_type, payload)`` tuples.

        Event types:
            ``audio`` — ``bytes`` of carrier mu-law 8 kHz audio, one 20 ms /
                160-byte frame per yield (transcoded from Gemini's
                ``output_sample_rate`` PCM)
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
                                mulaw = self._transcode_outbound_pcm_to_mulaw8(
                                    inline.data
                                )
                                for off in range(0, len(mulaw), MULAW_FRAME_BYTES):
                                    yield (
                                        GeminiLiveEventType.AUDIO.value,
                                        mulaw[off : off + MULAW_FRAME_BYTES],
                                    )
                            text = getattr(part, "text", None)
                            if text:
                                yield (
                                    GeminiLiveEventType.TRANSCRIPT_OUTPUT.value,
                                    text,
                                )
                    input_tx = getattr(server_content, "input_transcription", None)
                    if input_tx is not None and getattr(input_tx, "text", None):
                        yield (
                            GeminiLiveEventType.TRANSCRIPT_INPUT.value,
                            input_tx.text,
                        )
                    output_tx = getattr(server_content, "output_transcription", None)
                    if output_tx is not None and getattr(output_tx, "text", None):
                        yield (
                            GeminiLiveEventType.TRANSCRIPT_OUTPUT.value,
                            output_tx.text,
                        )
                    if getattr(server_content, "turn_complete", False):
                        yield (GeminiLiveEventType.RESPONSE_DONE.value, None)
                    if getattr(server_content, "interrupted", False):
                        yield (GeminiLiveEventType.SPEECH_STARTED.value, None)

                go_away = getattr(response, "go_away", None)
                if go_away is not None:
                    # Gemini Live hard-caps session length (~10-15 min without
                    # resumption); goAway is the only warning before the
                    # server drops the connection. Surface it loudly.
                    logger.warning(
                        "Gemini Live goAway received — session ends in %s",
                        getattr(go_away, "time_left", "unknown"),
                    )

                tool_call = getattr(response, "tool_call", None)
                if tool_call is not None:
                    for fn in getattr(tool_call, "function_calls", []) or []:
                        args = getattr(fn, "args", {}) or {}
                        call_id = getattr(fn, "id", "") or ""
                        fn_name = getattr(fn, "name", "") or ""
                        if call_id and fn_name:
                            self._pending_tool_calls[call_id] = fn_name
                        yield (
                            GeminiLiveEventType.FUNCTION_CALL.value,
                            {
                                "call_id": call_id,
                                "name": fn_name,
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
        self._pending_tool_calls.clear()
        self._inbound_resampler = None
        self._outbound_resampler_24to16 = None
        self._outbound_resampler_16to8 = None
