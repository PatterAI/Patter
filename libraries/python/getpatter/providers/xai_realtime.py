"""xAI Grok Voice Agent adapter for the Patter SDK (Beta).

Beta: validated against the xAI Voice Agent API spec (docs.x.ai); not yet
exercised against a live phone call.

The xAI Voice Agent API is OpenAI-Realtime-GA-compatible: same
``wss://.../v1/realtime?model=<model>`` handshake, same ``session.update`` /
event-name shapes, same function-call flow. So this adapter subclasses the GA
:class:`~getpatter.providers.openai_realtime_2.OpenAIRealtime2Adapter` and
changes only:

* the endpoint (``wss://api.x.ai/v1/realtime``, via the ``OPENAI_REALTIME_URL``
  class attribute the base ``connect()`` reads),
* the ``provider_key`` and the default model / voice (``grok-voice-latest`` /
  ``eve``),
* the ``session.update`` body — it grafts the xAI-only knobs (``reasoning``,
  ``audio.input.transcription.language_hint`` / ``keyterms``,
  ``audio.output.speed``, ``turn_detection.{threshold,prefix_padding_ms,
  idle_timeout_ms}``, ``replace``, ``resumption``, and server-side ``tools``)
  onto the GA config produced by the parent.

Audio transport is INHERITED UNCHANGED from the GA adapter: inbound mu-law
8 kHz is transcoded to PCM 24 kHz and outbound PCM 24 kHz back to mu-law 8 kHz.
xAI natively supports ``audio/pcmu`` @ 8 kHz, so a future optimization can flip
the session to native G.711 passthrough and skip the transcode — deferred to a
live-call validation pass.
"""

from __future__ import annotations

from typing import Any, ClassVar, Mapping, Sequence

from getpatter.providers.openai_realtime_2 import OpenAIRealtime2Adapter

# Default Grok voice model. ``grok-voice-latest`` tracks the current flagship
# (``grok-voice-think-fast-1.0`` at time of writing).
XAI_REALTIME_URL = "wss://api.x.ai/v1/realtime"
XAI_DEFAULT_MODEL = "grok-voice-latest"
XAI_DEFAULT_VOICE = "eve"
# xAI's input-transcription model. Setting it enables the
# ``conversation.item.input_audio_transcription.updated`` events (display-only);
# omitting the OpenAI-only ``whisper-1`` default keeps the session xAI-valid.
XAI_TRANSCRIPTION_MODEL = "grok-transcribe"


class XaiRealtimeAdapter(OpenAIRealtime2Adapter):
    """Realtime WebSocket adapter speaking xAI's Grok Voice Agent API (Beta).

    Subclasses :class:`OpenAIRealtime2Adapter` (GA-compatible) and overrides the
    endpoint, provider key, defaults, and ``session.update`` builder. All
    runtime behaviour — audio transcode, barge-in, tool dispatch, prewarm — is
    inherited unchanged.
    """

    #: xAI Grok Voice Agent WebSocket endpoint (base ``connect()`` reads this).
    OPENAI_REALTIME_URL: ClassVar[str] = XAI_REALTIME_URL
    #: Stable pricing/dashboard key — read by stream-handler/metrics.
    provider_key: ClassVar[str] = "xai_realtime"

    def __init__(
        self,
        *args: Any,
        vad_threshold: float | None = None,
        prefix_padding_ms: int | None = None,
        idle_timeout_ms: int | None = None,
        language_hint: str | None = None,
        keyterms: Sequence[str] = (),
        speed: float | None = None,
        replace: Mapping[str, str] | None = None,
        resumption: bool = False,
        server_tools: Sequence[Mapping[str, Any]] = (),
        **kwargs: Any,
    ) -> None:
        # xAI defaults; only apply when the caller left the slot unset so an
        # explicit model/voice/transcription-model always wins.
        kwargs.setdefault("model", XAI_DEFAULT_MODEL)
        kwargs.setdefault("voice", XAI_DEFAULT_VOICE)
        kwargs.setdefault("input_audio_transcription_model", XAI_TRANSCRIPTION_MODEL)
        # xAI-specific session knobs (grafted in ``_build_ga_session_config``).
        # ``reasoning_effort`` and ``silence_duration_ms`` are reused from the
        # base adapter and flow through ``**kwargs``.
        self.vad_threshold = vad_threshold
        self.prefix_padding_ms = prefix_padding_ms
        self.idle_timeout_ms = idle_timeout_ms
        self.language_hint = language_hint
        self.keyterms = tuple(keyterms)
        self.speed = speed
        self.replace = dict(replace) if replace else None
        self.resumption = resumption
        self.server_tools = tuple(server_tools)
        super().__init__(*args, **kwargs)

    def _build_ga_session_config(self) -> dict[str, Any]:
        """Build the GA session.update body, then graft the xAI-only knobs.

        Calls the parent GA builder (audio format, turn detection, instructions,
        voice, tools, reasoning) and adds the xAI extensions only when the caller
        set them, so an unconfigured session stays byte-compatible with the GA
        shape the xAI endpoint accepts.
        """
        config = super()._build_ga_session_config()

        input_cfg = config["audio"]["input"]
        transcription = input_cfg.setdefault("transcription", {})
        # xAI biases ASR via ``language_hint`` (BCP-47), NOT OpenAI's
        # ``language``. Drop the OpenAI-only key if the parent emitted it.
        transcription.pop("language", None)
        if self.language_hint is not None:
            transcription["language_hint"] = self.language_hint
        if self.keyterms:
            transcription["keyterms"] = list(self.keyterms)

        # Assistant playback speed multiplier (0.7–1.5).
        if self.speed is not None:
            config["audio"]["output"]["speed"] = self.speed

        # Turn-detection tuning — graft xAI knobs onto the GA turn_detection
        # block the parent produced (it always emits ``server_vad``).
        turn_detection = input_cfg.get("turn_detection")
        if isinstance(turn_detection, dict):
            if self.vad_threshold is not None:
                turn_detection["threshold"] = self.vad_threshold
            if self.prefix_padding_ms is not None:
                turn_detection["prefix_padding_ms"] = self.prefix_padding_ms
            if self.idle_timeout_ms is not None:
                turn_detection["idle_timeout_ms"] = self.idle_timeout_ms

        # Pronunciation replacements applied before TTS (transcript unchanged).
        if self.replace:
            config["replace"] = dict(self.replace)

        # Session resumption — cache + replay conversation turns on reconnect.
        if self.resumption:
            config["resumption"] = {"enabled": True}

        # xAI server-side tools (web_search, x_search, mcp, file_search) appended
        # verbatim to the session tools; xAI executes these on its side.
        if self.server_tools:
            config.setdefault("tools", [])
            config["tools"].extend(dict(t) for t in self.server_tools)

        return config
