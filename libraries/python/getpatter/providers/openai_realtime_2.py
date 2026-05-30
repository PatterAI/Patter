"""OpenAI Realtime adapter for the GA Realtime API (``gpt-realtime-2``).

``gpt-realtime-2`` is served from the same ``wss://api.openai.com/v1/realtime``
endpoint as the v1-beta family, but the GA endpoint:

- REJECTS the legacy ``OpenAI-Beta: realtime=v1`` header.
- REQUIRES ``session.type == "realtime"`` at the root of ``session.update``.
- Uses ``output_modalities`` (was ``modalities``).
- Nests audio config under ``audio.{input,output}`` with MIME ``type``
  strings (``audio/pcm``) instead of the v1 enum strings (``g711_ulaw``,
  ``pcm16``) and moves ``voice`` under ``audio.output.voice``,
  ``transcription`` + ``turn_detection`` under ``audio.input``.

Everything ELSE (event names, audio delta dispatch, barge-in / truncate
semantics, tool calling) is API-compatible with the v1 family — modulo a
small set of renamed events the GA API ships — so this adapter subclasses
:class:`OpenAIRealtimeAdapter` and overrides only :meth:`connect`,
:meth:`send_audio`, :meth:`send_first_message`, and the event-translation
layer. The runtime behaviour (``cancel_response``, ``send_text``,
``send_function_result``, ``close``) is inherited unchanged.

Note on audio transport
-----------------------
The GA endpoint accepts only PCM-16-LE with rate >= 24000 for both
``session.audio.input.format`` and ``session.audio.output.format``.
The ``audio/pcmu`` MIME type is accepted at the protocol level but the
server's audio engine silently drops mulaw frames — ``input_audio_buffer.commit``
returns "buffer only has 0.00ms of audio" and the call ends up muted.
Until OpenAI documents native g711_ulaw on the GA endpoint we transcode
on both directions on the Patter side:

- Inbound (Twilio/Telnyx → model): mulaw 8 kHz → PCM 24 kHz
- Outbound (model → Twilio/Telnyx): PCM 24 kHz → mulaw 8 kHz

The outbound path uses a stateful two-stage resampler (24k → 16k → 8k)
so phase carries across chunk boundaries and eliminates the click artefact
that a stateless helper would produce at every audio-delta boundary.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import struct
from typing import Any, AsyncGenerator

import websockets

from getpatter.audio.transcoding import (
    StatefulResampler,
    mulaw_to_pcm16,
    pcm16_to_mulaw,
)
from getpatter.providers.openai_realtime import (
    OpenAIRealtimeAdapter,
    OpenAIRealtimeVADType,
    OpenAITranscriptionModel,
)

logger = logging.getLogger("getpatter.openai_realtime_2")

__all__ = ["OpenAIRealtime2Adapter"]

# ---------------------------------------------------------------------------
# GA event name translation
# ---------------------------------------------------------------------------
# Mapping from GA Realtime event names back to the v1 names the rest of
# Patter (StreamHandler, metrics, dashboard) listens for. The GA API
# renamed several events but kept payload shapes identical, so we can
# translate at the WebSocket boundary and reuse the v1 event handler
# untouched.
_GA_TO_V1_EVENT_NAMES: dict[str, str] = {
    "response.output_audio.delta": "response.audio.delta",
    "response.output_audio.done": "response.audio.done",
    "response.output_audio_transcript.delta": "response.audio_transcript.delta",
    "response.output_audio_transcript.done": "response.audio_transcript.done",
}

# 20 ms of mulaw at 8 kHz = 160 bytes. Splitting large GA deltas into
# 160-byte frames gives the StreamHandler → bridge.send_audio chain the
# natural cadence it expects.
_MULAW_FRAME_BYTES = 160

# Gain boost applied to inbound telephony audio before upsampling to 24 kHz.
# The GA server VAD is calibrated against studio-quality 24 kHz audio;
# telephony-band mulaw typically sits at ~-12 dB peak relative to that.
# 2x gain lifts the signal into the VAD's expected range so speech_started
# fires reliably on phone-band input.
_INBOUND_GAIN = 2


class OpenAIRealtime2Adapter(OpenAIRealtimeAdapter):
    """Realtime WebSocket adapter speaking OpenAI's GA Realtime API.

    Subclasses :class:`OpenAIRealtimeAdapter` and overrides:

    - :meth:`connect` — omits ``OpenAI-Beta`` header; sends GA-shape
      ``session.update`` with nested ``audio.{input,output}`` and
      ``output_modalities``.
    - :meth:`send_audio` — transcodes inbound mulaw 8 kHz → PCM 24 kHz
      before appending to the input audio buffer.
    - :meth:`receive_events` — translates GA event names back to v1 names
      and decodes outbound PCM 24 kHz → mulaw 8 kHz in 20 ms slices.
    - :meth:`send_first_message` — uses ``output_modalities`` and re-injects
      ``audio.output.voice`` for the first response.create.

    Everything else (``cancel_response``, ``send_text``,
    ``send_function_result``, ``close``) is inherited unchanged.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Stateful two-stage outbound resampler: 24k → 16k → 8k.
        # Created lazily on the first audio delta so each session has its own state.
        self._outbound_resampler_24to16: StatefulResampler | None = None
        self._outbound_resampler_16to8: StatefulResampler | None = None
        # Last 8 kHz input sample carried across chunk boundaries for the
        # direct 3x linear upsample. The carry guarantees the first output of
        # each chunk interpolates from the real preceding sample, not from a
        # replicated first sample — without it every 20 ms Twilio frame
        # boundary becomes a small DC step that the GA server VAD interprets
        # as constant low-energy noise.
        self._inbound_8k_carry: int | None = None
        # Parked keepalive task created by open_parked_connection(). Tracked
        # here so close() can cancel it when the parked WS is abandoned
        # without adopt_websocket() being called (e.g. call dropped during
        # ringing). Cleared by adopt_websocket() or close().
        self._parked_keepalive_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_ga_session_config(self) -> dict[str, Any]:
        """Build the GA-shape session.update body."""
        # The GA endpoint requires audio/pcm with rate >= 24000 for both
        # directions. mulaw is not honoured by the audio engine even though
        # the protocol accepts the MIME type.
        fmt: dict[str, Any] = {"type": "audio/pcm", "rate": 24000}
        config: dict[str, Any] = {
            "type": "realtime",
            "output_modalities": self.modalities or ["audio"],
            "audio": {
                "input": {
                    "format": fmt,
                    "transcription": {
                        "model": self.input_audio_transcription_model
                        or OpenAITranscriptionModel.WHISPER_1.value,
                    },
                    # VAD threshold raised back to the OpenAI default (0.5)
                    # on 2026-05-22. The earlier 0.1 tuning (motivated by
                    # the upsampled telephony-band loss in high frequencies)
                    # made the server VAD trigger on the carrier-loopback
                    # echo of the agent's OWN outbound audio in PSTN no-AEC
                    # scenarios. Combined with the default
                    # ``turn_detection.create_response: true``, every phantom
                    # ``speech_started`` ended a turn early and auto-created
                    # a new response that the agent immediately spoke over,
                    # leading to a runaway loop where the first message was
                    # repeatedly cut and re-generated.
                    "turn_detection": {
                        "type": self.vad_type or OpenAIRealtimeVADType.SERVER_VAD.value,
                        "threshold": 0.5,
                        "prefix_padding_ms": 300,
                        "silence_duration_ms": self.silence_duration_ms,
                        # Defer ``response.create`` to the application: when
                        # OpenAI's server VAD commits an
                        # ``input_audio_buffer.committed`` segment that turns
                        # out to be a Whisper hallucination on silence/echo,
                        # auto-creating a response would generate a phantom
                        # turn (the model reads the hallucinated text as user
                        # input). Patter triggers ``response.create``
                        # explicitly in the Realtime stream-handler AFTER
                        # validating ``transcript_input`` against the
                        # hallucination filter. Pair with
                        # ``interrupt_response: false`` so server VAD also
                        # leaves in-flight responses alone — barge-in is
                        # gated client-side.
                        "create_response": False,
                        "interrupt_response": False,
                    },
                },
                "output": {
                    "format": fmt,
                    "voice": self.voice,
                },
            },
            "instructions": self.instructions
            or f"You are a helpful voice assistant. Respond in {self.language}. Be concise and natural.",
        }
        if self.temperature is not None:
            config["temperature"] = self.temperature
        if self.max_response_output_tokens is not None:
            config["max_output_tokens"] = self.max_response_output_tokens
        if self.tool_choice is not None:
            config["tool_choice"] = self.tool_choice
        if self.reasoning_effort is not None:
            config["reasoning"] = {"effort": self.reasoning_effort}
        if self.tools:
            config["tools"] = [self._build_tool_wire_format(t) for t in self.tools]
        return config

    def _transcode_inbound_mulaw8_to_pcm24(self, mulaw: bytes) -> bytes:
        """mulaw 8 kHz → PCM-16-LE 24 kHz via direct 3x linear interpolation.

        For every consecutive pair of 8 kHz samples (s_a, s_b) we emit three
        24 kHz samples::

            out_0 = s_a
            out_1 = round(2/3·s_a + 1/3·s_b)
            out_2 = round(1/3·s_a + 2/3·s_b)

        A one-sample carry across chunk boundaries eliminates the DC step at
        every 20 ms Twilio frame boundary that otherwise causes the GA server
        VAD to read constant low-energy noise and never fire speech_started.
        The first chunk (no carry yet) loses 3 output samples at the leading
        edge (~375 µs), which is well below any audible artefact and well
        below the VAD's 300 ms prefix-padding window.

        A 2x gain boost lifts telephony-band audio (~-12 dB peak) into the
        range the GA VAD was calibrated against. Int16 values are clamped to
        ±32767 to avoid wrap-around.
        """
        pcm8 = mulaw_to_pcm16(mulaw)
        num_samples = len(pcm8) // 2
        if num_samples == 0:
            return b""

        # Unpack all 8 kHz samples at once and apply gain.
        samples8 = [
            max(
                -32768,
                min(32767, struct.unpack_from("<h", pcm8, i * 2)[0] * _INBOUND_GAIN),
            )
            for i in range(num_samples)
        ]

        # Prepend carry from previous chunk.
        if self._inbound_8k_carry is not None:
            inputs = [self._inbound_8k_carry] + samples8
        else:
            inputs = samples8

        # Save last sample for the next chunk.
        self._inbound_8k_carry = inputs[-1]

        num_pairs = len(inputs) - 1
        if num_pairs <= 0:
            return b""

        # Emit 3 output samples per input pair.
        out = bytearray(num_pairs * 3 * 2)
        for i in range(num_pairs):
            s0 = inputs[i]
            s1 = inputs[i + 1]
            offset = i * 6
            struct.pack_into("<h", out, offset, s0)
            struct.pack_into("<h", out, offset + 2, round((s0 * 2 + s1) / 3))
            struct.pack_into("<h", out, offset + 4, round((s0 + s1 * 2) / 3))
        return bytes(out)

    def _transcode_outbound_pcm24_to_mulaw8(self, delta_b64: str) -> bytes:
        """Base64 PCM-16-LE 24 kHz → mulaw 8 kHz.

        Uses a stateful two-stage resampler (24k → 16k → 8k) to eliminate
        boundary clicks. The 16k→8k stage uses audioop's built-in anti-alias
        filter which removes energy above 4 kHz before decimation, preventing
        aliasing artefacts that a direct 3:1 decimator would produce on the
        voice content emitted by gpt-realtime-2.
        """
        if self._outbound_resampler_24to16 is None:
            self._outbound_resampler_24to16 = StatefulResampler(
                src_rate=24000, dst_rate=16000
            )
            self._outbound_resampler_16to8 = StatefulResampler(
                src_rate=16000, dst_rate=8000
            )
        pcm24 = base64.b64decode(delta_b64)
        pcm16 = self._outbound_resampler_24to16.process(pcm24)
        pcm8 = self._outbound_resampler_16to8.process(pcm16)  # type: ignore[union-attr]
        if not pcm8:
            return b""
        return pcm16_to_mulaw(pcm8)

    def _translate_ga_event(self, raw: str) -> list[str]:
        """Translate a raw GA JSON frame to a list of v1-compatible JSON frames.

        For ``response.output_audio.delta`` frames the outbound PCM 24 kHz
        audio is transcoded to mulaw 8 kHz and split into 20 ms slices
        (160 bytes each), each yielded as a separate ``response.audio.delta``
        frame. All other GA-renamed events are rewritten to their v1 name.
        Returns the unmodified raw string if no translation is needed.
        """
        try:
            data = json.loads(raw)
        except Exception:
            return [raw]

        event_type = data.get("type", "")

        if event_type == "response.output_audio.delta":
            delta_b64: str = data.get("delta", "")
            if not isinstance(delta_b64, str):
                return [raw]
            mulaw = self._transcode_outbound_pcm24_to_mulaw8(delta_b64)
            if not mulaw:
                return []  # resampler warmup — no output yet
            frames: list[str] = []
            for off in range(0, len(mulaw), _MULAW_FRAME_BYTES):
                chunk = mulaw[off : off + _MULAW_FRAME_BYTES]
                frame = dict(data)
                frame["type"] = "response.audio.delta"
                frame["delta"] = base64.b64encode(chunk).decode("ascii")
                frames.append(json.dumps(frame))
            return frames

        v1_name = _GA_TO_V1_EVENT_NAMES.get(event_type)
        if v1_name is not None:
            data["type"] = v1_name
            return [json.dumps(data)]

        return [raw]

    # ------------------------------------------------------------------
    # Overridden public methods
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Connect to the GA Realtime endpoint and apply the GA session config.

        Differences from the v1 ``connect()``:

        - Header ``OpenAI-Beta: realtime=v1`` is OMITTED.
        - ``session.update`` uses the GA shape: nested ``audio.{input,output}``,
          ``output_modalities``, ``session.type == "realtime"``.
        - Surfaces real GA-side rejection errors (``invalid_model``,
          ``missing_required_parameter``) immediately instead of timing out.
        """
        url = f"{self.OPENAI_REALTIME_URL}?model={self.model}"
        self._ws = await websockets.connect(
            url,
            additional_headers={
                "Authorization": f"Bearer {self.api_key}",
            },
            ping_interval=20,
            ping_timeout=20,
        )
        self._running = True

        try:
            # Wait for session.created.
            raw = await asyncio.wait_for(self._ws.recv(), timeout=15.0)
            data = json.loads(raw)
            if data.get("type") == "error":
                err = data.get("error") or {}
                msg = err.get("message") or err.get("code") or str(data)
                raise RuntimeError(f"OpenAI Realtime 2 setup error: {msg}")
            if data.get("type") != "session.created":
                err = data.get("error") or {}
                msg = err.get("message") or err.get("code") or str(data)
                raise RuntimeError(
                    f"Expected session.created, got {data.get('type')!r}: {msg}"
                )

            await self._ws.send(
                json.dumps(
                    {
                        "type": "session.update",
                        "session": self._build_ga_session_config(),
                    }
                )
            )

            # Wait for session.updated, surface any error immediately.
            await self._await_session_updated_ga()

        except Exception:
            await self._ws.close()
            self._ws = None
            self._running = False
            raise

    async def _await_session_updated_ga(self) -> None:
        """Wait for ``session.updated``, raising on ``error`` events."""
        deadline = asyncio.get_event_loop().time() + self._SESSION_UPDATE_TIMEOUT
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                logger.warning(
                    "OpenAI Realtime 2: no session.updated received after %.1fs; "
                    "continuing anyway",
                    self._SESSION_UPDATE_TIMEOUT,
                )
                return
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
            except TimeoutError:
                logger.warning(
                    "OpenAI Realtime 2: no session.updated received after %.1fs; "
                    "continuing anyway",
                    self._SESSION_UPDATE_TIMEOUT,
                )
                return
            try:
                data = json.loads(raw)
            except Exception:
                continue
            if data.get("type") == "session.updated":
                return
            if data.get("type") == "error":
                err = data.get("error") or {}
                msg = err.get("message") or err.get("code") or str(data)
                raise RuntimeError(f"OpenAI Realtime 2 setup error: {msg}")
            # Any other event gets buffered for the normal receive loop.
            self._pending_events.append(raw)

    async def open_parked_connection(self):  # type: ignore[no-untyped-def]
        """Open a fresh GA Realtime WS during ringing, prime
        ``session.update`` / ``session.updated``, and return the OPEN
        socket WITHOUT taking it on ``self._ws``.

        Used by the prewarm pipeline to park a Realtime connection
        during the carrier ringing window so the per-call StreamHandler
        can adopt a fully-primed session at carrier ``start`` —
        eliminating the TCP + TLS + HTTP-101 + ``session.update`` ack
        round-trip from the critical path. Saves ~300-600 ms of
        first-audible-word latency on outbound.

        Bounded by 8 s (matches the legacy v1 adapter). Raises on
        timeout / handshake failure / GA-side rejection — the prewarm
        pipeline treats any error as a cache miss and the call falls
        through to the cold :meth:`connect` path.

        Billing safety: confirmed by OpenAI's Managing Realtime Costs
        guide — ``session.update`` does NOT invoke the model and bills
        no tokens. An idle parked socket costs $0. Call-completion /
        no-answer paths drain the slot via ``_close_parked_slot``.

        Override of the legacy v1 adapter's parker so the GA shape
        (``session.type == "realtime"``, ``output_modalities``, nested
        ``audio.{input,output}``) is sent instead of the v1-beta flat
        shape.
        """
        url = f"{self.OPENAI_REALTIME_URL}?model={self.model}"
        # Aggressive ping cadence on the parked socket. OpenAI's GA
        # Realtime endpoint closes idle sockets within ~5-7 s when no
        # frames are seen — the protocol-level WS PING is enough to
        # keep the session alive between park (~T0) and adopt (whenever
        # the callee picks up, typically T+3-15 s on cellular). 4 s
        # cadence guarantees at least one ping reaches the server
        # before the idle-disconnect window fires. The live session
        # keeps the parent's 20 / 20 default — once the call is live,
        # bidirectional audio frames are themselves the keepalive.
        ws = await asyncio.wait_for(
            websockets.connect(
                url,
                additional_headers={
                    "Authorization": f"Bearer {self.api_key}",
                },
                ping_interval=4,
                ping_timeout=4,
            ),
            timeout=8.0,
        )
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            data = json.loads(raw)
            if data.get("type") != "session.created":
                err = data.get("error") or {}
                msg = err.get("message") or err.get("code") or str(data)
                raise RuntimeError(
                    f"Expected session.created on parked GA WS, "
                    f"got {data.get('type')!r}: {msg}"
                )
            await ws.send(
                json.dumps(
                    {
                        "type": "session.update",
                        "session": self._build_ga_session_config(),
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
                if isinstance(data, dict) and data.get("type") == "error":
                    err = data.get("error") or {}
                    msg = err.get("message") or err.get("code") or str(data)
                    raise RuntimeError(f"OpenAI Realtime 2 parked-setup error: {msg}")
        except Exception:
            try:
                await ws.close()
            except Exception:
                pass
            raise
        # Application-level keepalive. Empirically, OpenAI's GA Realtime
        # edge closes idle parked sockets within ~6-7 s even with a 4 s
        # WS-level ping — protocol PINGs alone are not counted as
        # activity. Sending an idempotent ``session.update`` every 3 s
        # is the documented "ping" pattern (re-affirms session config,
        # bills no tokens) and reliably keeps the socket alive across
        # the 3-15 s ringing window. Task also drains incoming acks so
        # the receive buffer doesn't back-pressure the writer. Cancelled
        # by :meth:`adopt_websocket` when the live adapter takes over.
        keepalive_task = asyncio.create_task(
            self._parked_keepalive_loop(ws),
            name=f"openai-realtime-parked-keepalive:{id(ws)}",
        )
        # Store on self so close() can cancel if the parked WS is abandoned
        # without adopt_websocket() (e.g. call dropped during ringing).
        self._parked_keepalive_task = keepalive_task
        attached = False
        try:
            ws._parked_keepalive_task = keepalive_task  # type: ignore[attr-defined]
            attached = True
        except Exception as exc:
            logger.info(
                "[PREWARM-KA] setattr failed: %s — task still tracked on self", exc
            )
        logger.info(
            "[PREWARM-KA] task scheduled attached=%s ws_id=%s", attached, id(ws)
        )
        return ws

    async def _parked_keepalive_loop(self, ws) -> None:  # type: ignore[no-untyped-def]
        """Drain incoming frames and emit a no-op ``session.update``
        every 3 s on the parked GA Realtime WS until cancelled."""
        logger.info("[PREWARM-KA] loop started ws_id=%s", id(ws))
        next_ping = asyncio.get_event_loop().time() + 3.0
        pings_sent = 0
        try:
            while True:
                now = asyncio.get_event_loop().time()
                wait_for = max(0.0, next_ping - now)
                try:
                    await asyncio.wait_for(ws.recv(), timeout=wait_for)
                    continue
                except asyncio.TimeoutError:
                    pass
                except Exception as exc:
                    logger.info(
                        "[PREWARM-KA] recv died after %d pings: %s",
                        pings_sent,
                        exc,
                    )
                    return
                try:
                    await ws.send(
                        json.dumps(
                            {
                                "type": "session.update",
                                "session": self._build_ga_session_config(),
                            }
                        )
                    )
                    pings_sent += 1
                    logger.info(
                        "[PREWARM-KA] sent session.update #%d ws_closed=%s",
                        pings_sent,
                        getattr(ws, "closed", "?"),
                    )
                except Exception as exc:
                    logger.info(
                        "[PREWARM-KA] send failed after %d pings: %s",
                        pings_sent,
                        exc,
                    )
                    return
                next_ping = asyncio.get_event_loop().time() + 3.0
        except asyncio.CancelledError:
            logger.info(
                "[PREWARM-KA] cancelled after %d pings (adopted or closed)",
                pings_sent,
            )
            raise

    def adopt_websocket(self, ws) -> None:  # type: ignore[no-untyped-def]
        """Adopt a pre-opened, already-``session.updated`` GA Realtime WS
        produced by the prewarm pipeline. Skips the cold-connect path —
        saves ~300-600 ms on first audible word.

        Caller MUST verify the WS is still alive before calling and
        MUST have already received ``session.updated`` on the parked
        socket. If the parked WS died between park and adopt, fall back
        to :meth:`connect`. Parity with parent ``adopt_websocket`` but
        explicit here so the override surfaces in the GA adapter's
        public API (and so tooling that introspects the adapter sees
        the method without walking the MRO).
        """
        # Cancel the parked keepalive loop — the live receive_events()
        # owns the WS from here on. Awaiting cancellation isn't possible
        # in a sync method; the loop tolerates abrupt cancellation
        # (it raises CancelledError out of its single recv()/send()).
        ka = getattr(ws, "_parked_keepalive_task", None)
        if ka is not None:
            try:
                ka.cancel()
            except Exception:
                pass
            try:
                delattr(ws, "_parked_keepalive_task")
            except Exception:
                pass
        # Also clear the self-tracked reference so close() doesn't double-cancel.
        self._parked_keepalive_task = None
        self._ws = ws
        self._running = True

    async def close(self) -> None:
        """Cancel any parked keepalive task then delegate to the parent close().

        The parent's close() only cancels ``self._receive_task``. If
        ``open_parked_connection()`` was called but the parked WS was
        abandoned without ``adopt_websocket()`` (e.g. call dropped during
        ringing), the keepalive task would otherwise keep running until the
        remote end closes the WS (~3-15 s). This override ensures it is
        cancelled immediately.
        """
        ka = self._parked_keepalive_task
        if ka is not None and not ka.done():
            ka.cancel()
            try:
                await ka
            except (asyncio.CancelledError, Exception):
                pass
        self._parked_keepalive_task = None
        await super().close()

    async def send_audio(self, audio: bytes) -> None:
        """Send audio to the GA Realtime API.

        Transcodes inbound mulaw 8 kHz (from Twilio/Telnyx) to PCM-16-LE
        24 kHz before appending to the input audio buffer. The GA server's
        audio engine ignores mulaw frames even though it accepts ``audio/pcmu``
        at the protocol level — raw mulaw results in "buffer only has 0.00ms
        of audio" and a muted call.
        """
        if self._ws is None:
            return
        pcm24 = self._transcode_inbound_mulaw8_to_pcm24(audio)
        if not pcm24:
            return
        encoded = base64.b64encode(pcm24).decode("ascii")
        await self._ws.send(
            json.dumps({"type": "input_audio_buffer.append", "audio": encoded})
        )

    async def receive_events(self) -> AsyncGenerator[tuple[str, Any], None]:
        """Yield events from the GA Realtime API, translating event names to v1.

        Outbound audio deltas (``response.output_audio.delta``) are:
        1. Transcoded from PCM 24 kHz → mulaw 8 kHz.
        2. Split into 20 ms / 160-byte slices and emitted as individual
           ``("audio", bytes)`` events so StreamHandler's cadence is
           preserved.

        Other GA-renamed events are translated to their v1 equivalents
        before dispatch.
        """
        if self._ws is None:
            return

        import websockets.exceptions as _ws_exc

        async def _iter_raw():
            while self._pending_events:
                yield self._pending_events.popleft()
            async for msg in self._ws:
                yield msg

        try:
            async for raw in _iter_raw():
                # Translate GA event names / audio format.
                translated_frames = self._translate_ga_event(raw)
                for frame in translated_frames:
                    # Delegate actual event parsing to a helper so we don't
                    # duplicate the full dispatch table from the parent class.
                    async for event in self._dispatch_frame(frame):
                        yield event
        except _ws_exc.ConnectionClosed as exc:
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

    async def _dispatch_frame(self, raw: str) -> AsyncGenerator[tuple[str, Any], None]:
        """Parse and dispatch a single (already translated) JSON frame.

        This is a subset of the parent's ``receive_events`` dispatch table,
        re-used after GA→v1 name translation so we don't duplicate logic.
        """
        import time as _time

        try:
            data = json.loads(raw)
        except Exception:
            return
        event_type = data.get("type", "")

        if event_type == "response.audio.delta":
            audio_bytes = base64.b64decode(data.get("delta", ""))
            # For GA path the audio is already mulaw 8 kHz (transcoded in
            # _translate_ga_event). Use the mulaw estimator (8 bytes/ms).
            self._current_response_audio_ms += len(audio_bytes) // 8
            if self._current_response_first_audio_at is None:
                self._current_response_first_audio_at = _time.monotonic()
            yield ("audio", audio_bytes)

        elif event_type == "response.audio_transcript.delta":
            yield ("transcript_output", data.get("delta", ""))

        elif event_type in (
            "response.content_part.added",
            "response.output_item.added",
        ):
            item = data.get("item") or {}
            item_id = item.get("id") or data.get("item_id")
            if item_id:
                self._current_response_item_id = item_id
                self._current_response_audio_ms = 0
                self._current_response_first_audio_at = None

        elif event_type == "input_audio_buffer.speech_started":
            yield ("speech_started", None)

        elif event_type == "input_audio_buffer.speech_stopped":
            yield ("speech_stopped", None)

        elif event_type == "conversation.item.input_audio_transcription.completed":
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
            err = data.get("error", {})
            logger.error("OpenAI Realtime 2 error: %s", err)
            yield ("error", err)

    async def send_first_message(self, text: str) -> None:
        """Make the AI speak ``text`` as its opening line using GA-shape fields.

        Two differences from the v1 path:

        1. Uses ``output_modalities`` (the GA endpoint rejects
           ``response.modalities``).
        2. Re-injects ``audio.output.voice`` — the GA ``response.create``
           does NOT inherit voice from the session for this explicit request;
           it falls back to the server-side default (``marin``, female) when
           the field is omitted.
        """
        if self._ws is None:
            return
        response_body: dict[str, Any] = {
            "output_modalities": ["audio"],
            "audio": {"output": {"voice": self.voice}},
            "instructions": (
                f"Say exactly the following sentence as your first turn "
                f'and nothing else: "{text}"'
            ),
        }
        # ``reasoning.effort`` is only accepted by the flagship GA
        # variants (``gpt-realtime``, ``gpt-realtime-2``, …) — the
        # cost-tier ``gpt-realtime-mini`` rejects it as "Unsupported
        # option for this model". Forward the field only when the
        # caller explicitly configured it.
        if self.reasoning_effort is not None:
            response_body["reasoning"] = {"effort": self.reasoning_effort}
        await self._ws.send(
            json.dumps({"type": "response.create", "response": response_body})
        )
