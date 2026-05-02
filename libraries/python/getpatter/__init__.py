"""getpatter — open-source voice AI SDK.

Installation extras:

* Base: ``pip install getpatter`` — core telephony + OpenAI Realtime + pipeline
  mode with Deepgram STT and ElevenLabs TTS.
* ``scheduling`` — APScheduler-backed ``schedule_cron`` / ``schedule_once`` /
  ``schedule_interval`` helpers. Install with
  ``pip install 'getpatter[scheduling]'``. Calling a scheduler helper without
  this extra raises ``RuntimeError`` at call time (by design — the SDK does
  not ship APScheduler in the base install to keep the default footprint
  small).
* Optional provider extras (``anthropic``, ``groq``, ``cerebras``, ``google``,
  ``gemini-live``, ``ultravox``, ``speechmatics``, ``assemblyai``, ``cartesia``,
  ``soniox``, ``rime``, ``lmnt``, ``telnyx-ai``, ``silero``, ``krisp``,
  ``deepfilternet``, ``ivr``, ``background-audio``, ``evals``, ``tracing``) —
  install only the ones matching the provider your agent uses.

See ``pyproject.toml`` and the top-level README for the full matrix.
"""

__version__ = "0.5.5"

from getpatter.client import Patter
from getpatter.models import (
    Agent,
    CallControl,
    CallEvent,
    CallMetrics,
    CostBreakdown,
    Guardrail,
    HookContext,
    IncomingMessage,
    LatencyBreakdown,
    PipelineHooks,
    STTConfig,
    TTSConfig,
    TurnMetrics,
)
from getpatter.exceptions import (
    ErrorCode,
    PatterError,
    PatterConnectionError,
    AuthenticationError,
    ProvisionError,
    RateLimitError,
)
from getpatter.services.sentence_chunker import SentenceChunker
from getpatter.services.pipeline_hooks import PipelineHookExecutor
from getpatter.services.text_transforms import (
    filter_markdown,
    filter_emoji,
    filter_for_tts,
)

# New v0.5.0 public API (Phase 1a). ``tool`` here is the unified factory that
# supports both decorator use (``@tool`` on a typed function) and keyword
# construction (``tool(name=..., handler=...)``). It supersedes the historical
# :func:`getpatter.tools.tool_decorator.tool` at the top level, but that module
# remains importable for users that already depend on the legacy dict shape.
from getpatter._public_api import Tool, tool, guardrail

# Flat aliases for the 4-line quickstart.
from getpatter.carriers.twilio import Carrier as Twilio
from getpatter.carriers.telnyx import Carrier as Telnyx
from getpatter.engines.openai import Realtime as OpenAIRealtime
from getpatter.engines.elevenlabs import ConvAI as ElevenLabsConvAI

# STT flat aliases — parity with libraries/typescript/src/index.ts.
from getpatter.stt.deepgram import STT as DeepgramSTT
from getpatter.stt.whisper import STT as WhisperSTT
from getpatter.stt.openai_transcribe import STT as OpenAITranscribeSTT
from getpatter.stt.cartesia import STT as CartesiaSTT
from getpatter.stt.soniox import STT as SonioxSTT
from getpatter.stt.speechmatics import STT as SpeechmaticsSTT
from getpatter.stt.assemblyai import STT as AssemblyAISTT

# TTS flat aliases.
from getpatter.tts.elevenlabs import TTS as ElevenLabsTTS
from getpatter.tts.elevenlabs_ws import TTS as ElevenLabsWebSocketTTS
from getpatter.tts.openai import TTS as OpenAITTS
from getpatter.tts.cartesia import TTS as CartesiaTTS
from getpatter.tts.rime import TTS as RimeTTS
from getpatter.tts.lmnt import TTS as LMNTTTS

# LLM flat aliases — parity with libraries/typescript/src/index.ts and mirror of STT/TTS layout.
from getpatter.llm.openai import LLM as OpenAILLM
from getpatter.llm.anthropic import LLM as AnthropicLLM
from getpatter.llm.groq import LLM as GroqLLM
from getpatter.llm.cerebras import LLM as CerebrasLLM
from getpatter.llm.google import LLM as GoogleLLM

# Telephony adapters — surface for tests + advanced integrations that need
# direct access to provider-specific APIs (e.g. custom webhook wiring).
from getpatter.providers.twilio_adapter import TwilioAdapter
from getpatter.providers.telnyx_adapter import TelnyxAdapter


# VAD — opt-in (needs the ``silero`` extra: numpy + onnxruntime). Loaded
# lazily so importing ``getpatter`` doesn't require those native deps.
def __getattr__(name):
    if name == "SileroVAD":
        from getpatter.providers.silero_vad import SileroVAD as _SileroVAD

        return _SileroVAD
    if name in {"SileroSampleRate", "SileroVADEventType", "SileroVADProviderTag"}:
        from getpatter.providers import silero_vad as _silero_vad

        return getattr(_silero_vad, name)
    if name in {
        "KrispSampleRate",
        "KrispFrameDuration",
    }:
        from getpatter.providers import krisp_instance as _krisp_instance

        return getattr(_krisp_instance, name)
    if name in {"OnnxExecutionProvider", "SileroOnnxSampleRate"}:
        from getpatter.providers import silero_onnx as _silero_onnx

        return getattr(_silero_onnx, name)
    raise AttributeError(f"module 'getpatter' has no attribute {name!r}")


# Observability — opt-in OTel tracing.
from getpatter.observability import (
    init_tracing,
    start_span,
    SPAN_CALL,
    SPAN_STT,
    SPAN_LLM,
    SPAN_TTS,
    SPAN_TOOL,
    SPAN_ENDPOINT,
    SPAN_BARGEIN,
)

# `is_tracing_enabled` is the public top-level alias for parity with TypeScript
# `isTracingEnabled`. The Python implementation lives at
# ``getpatter.observability.tracing.is_enabled``.
from getpatter.observability.tracing import is_enabled as is_tracing_enabled
from getpatter.observability.event_bus import EventBus, PatterEventType

# Tunnel flat aliases. ``Static`` is the canonical name (matches the docstring
# examples and the ``tunnel=Static(hostname=...)`` pattern); ``StaticTunnel``
# is kept as an alias for symmetry with TypeScript and ``CloudflareTunnel``.
from getpatter.tunnels import CloudflareTunnel, Ngrok, Static

StaticTunnel = Static

from getpatter.services.fallback_provider import (
    FallbackLLMProvider,
    AllProvidersFailedError,
    PartialStreamError,
)
from getpatter.services.chat_context import ChatContext, ChatMessage
from getpatter.services.ivr import (
    DtmfEvent,
    IVRActivity,
    TfidfLoopDetector,
    format_dtmf,
)
from getpatter.scheduler import (
    ScheduleHandle,
    schedule_cron,
    schedule_once,
    schedule_interval,
)

# Dashboard / metrics — parity with TypeScript ``MetricsStore``,
# ``mountDashboard``, ``mountApi``, ``notifyDashboard``,
# ``makeAuthMiddleware``, ``callsToCsv``, ``callsToJson``.
from getpatter.dashboard.store import MetricsStore
from getpatter.dashboard.routes import mount_dashboard
from getpatter.api_routes import mount_api
from getpatter.dashboard.persistence import notify_dashboard
from getpatter.dashboard.auth import make_auth_dependency

# TS exposes a single `makeAuthMiddleware`; in Python the FastAPI-idiomatic
# equivalent is the dependency factory. Alias it as `make_auth_middleware`
# for parity with the TS public surface — both names point at the same
# callable so call sites coming from TS docs keep working.
make_auth_middleware = make_auth_dependency
from getpatter.dashboard.export import calls_to_csv, calls_to_json

# LLM loop primitives — parity with TypeScript ``LLMLoop``,
# ``OpenAILLMProvider``, the ``LLMProvider`` protocol, ``DefaultToolExecutor``,
# and the ``LLMChunk`` streaming-output type.
from getpatter.services.llm_loop import (
    DefaultToolExecutor,
    LLMChunk,
    LLMLoop,
    LLMProvider,
    OpenAILLMProvider,
)

# Remote-message + test session helpers.
from getpatter.services.remote_message import (
    RemoteMessageHandler,
    is_remote_url,
    is_websocket_url,
)
from getpatter.test_mode import TestSession

# Background-audio primitives — parity with TypeScript
# ``BackgroundAudioPlayer`` / ``BuiltinAudioClip`` / ``mixPcm`` /
# ``selectSoundFromList`` / ``builtinClipPath``.
from getpatter.audio.background_audio import (
    BackgroundAudioPlayer,
    BuiltinAudioClip,
    builtin_clip_path,
    select_sound_from_list,
)

# Audio transcoding helpers — parity with the TypeScript ``transcoding``
# module. Python ships ``create_resampler_24k_to_16k`` only (no eager
# ``resample_24k_to_16k`` one-shot helper exists yet).
from getpatter.audio.transcoding import (
    PcmCarry,
    StatefulResampler,
    create_resampler_8k_to_16k,
    create_resampler_16k_to_8k,
    create_resampler_24k_to_16k,
    create_resampler_24k_to_8k,
    mulaw_to_pcm16,
    pcm16_to_mulaw,
    resample_8k_to_16k,
    resample_16k_to_8k,
    resample_24k_to_16k,
)

# Pricing helpers — parity with TypeScript ``DEFAULT_PRICING``,
# ``mergePricing``, ``calculateSttCost``, ``calculateTtsCost``,
# ``calculateRealtimeCost``, ``calculateTelephonyCost``.
from getpatter.pricing import (
    DEFAULT_PRICING,
    calculate_realtime_cost,
    calculate_stt_cost,
    calculate_telephony_cost,
    calculate_tts_cost,
    merge_pricing,
)

# Per-call metrics accumulator (TypeScript: ``CallMetricsAccumulator``).
from getpatter.services.metrics import CallMetricsAccumulator


# Top-level re-export for parity with TypeScript ``mixPcm`` (see BUG #04g).
# Import is lazy — `mix_pcm` triggers numpy import only on first call.
def mix_pcm(agent: bytes, bg: bytes, ratio: float) -> bytes:
    """Standalone PCM mixer — parity with TypeScript ``mixPcm(agent, bg, ratio)``."""
    from getpatter.audio.pcm_mixer import mix_pcm as _mix_pcm

    return _mix_pcm(agent, bg, ratio)


# Integrations adapter for external agent frameworks (Hermes, OpenAI, etc.).
from getpatter.integrations import PatterTool, PatterToolResult  # noqa: E402

# ----------------------------------------------------------------------
# Provider-specific enums
# ----------------------------------------------------------------------
# Centralised re-export of per-provider ``StrEnum``/``IntEnum`` symbols so
# user code gets autocomplete and type-checker support from the public
# package surface (``from getpatter import DeepgramModel, …``). Mirrors the
# TypeScript ``index.ts`` re-exports.
from getpatter.providers.deepgram_stt import (  # noqa: E402
    DeepgramEncoding,
    DeepgramModel,
    DeepgramSampleRate,
)
from getpatter.providers.elevenlabs_tts import (  # noqa: E402
    ElevenLabsModel,
    ElevenLabsOutputFormat,
)
from getpatter.providers.elevenlabs_ws_tts import (  # noqa: E402
    ElevenLabsWSField,
    ElevenLabsWSServerError,
)
from getpatter.providers.assemblyai_stt import (  # noqa: E402
    AssemblyAIClientFrame,
    AssemblyAIDomain,
    AssemblyAIEncoding,
    AssemblyAIEventType,
    AssemblyAIModel,
    AssemblyAISampleRate,
)
from getpatter.providers.soniox_stt import (  # noqa: E402
    SonioxAudioFormat,
    SonioxClientFrame,
    SonioxEndpointToken,
    SonioxModel,
    SonioxSampleRate,
)
from getpatter.providers.speechmatics_stt import (  # noqa: E402
    SpeechmaticsAudioEncoding,
    SpeechmaticsOperatingPoint,
    SpeechmaticsSampleRate,
    TurnDetectionMode,
)
from getpatter.providers.cartesia_stt import (  # noqa: E402
    CartesiaSTTClientFrame,
    CartesiaSTTEncoding,
    CartesiaSTTModel,
    CartesiaSTTSampleRate,
    CartesiaSTTServerEvent,
)
from getpatter.providers.telnyx_stt import (  # noqa: E402
    TelnyxSTTInputFormat,
    TelnyxSTTSampleRate,
    TelnyxTranscriptionEngine,
)
from getpatter.providers.whisper_stt import (  # noqa: E402
    WhisperModel,
    WhisperResponseFormat,
)
from getpatter.providers.openai_tts import (  # noqa: E402
    OpenAITTSModel,
    OpenAITTSResponseFormat,
    OpenAITTSVoice,
)
from getpatter.providers.telnyx_tts import (  # noqa: E402
    TelnyxTTSSampleRate,
    TelnyxTTSVoice,
)
from getpatter.providers.gemini_live import (  # noqa: E402
    GeminiLiveApiVersion,
    GeminiLiveEventType,
    GeminiLiveModel,
    GeminiLiveResponseModality,
    GeminiLiveSampleRate,
    GeminiLiveVoice,
)
from getpatter.providers.ultravox_realtime import (  # noqa: E402
    UltravoxAdapterEvent,
    UltravoxClientFrame,
    UltravoxFirstSpeaker,
    UltravoxMessageRole,
    UltravoxModel,
    UltravoxOutputMedium,
    UltravoxParameterLocation,
    UltravoxSampleRate,
    UltravoxServerEvent,
    UltravoxState,
)
# OnnxExecutionProvider, SileroOnnxSampleRate exposed lazily via __getattr__
# above (requires the optional ``silero`` extra: numpy + onnxruntime).

__all__ = [
    "Patter",
    "Agent",
    "CallControl",
    "CallEvent",
    "CallMetrics",
    "CostBreakdown",
    "Guardrail",
    "HookContext",
    "IncomingMessage",
    "LatencyBreakdown",
    "PipelineHooks",
    "STTConfig",
    "TTSConfig",
    "TurnMetrics",
    "ErrorCode",
    "PatterError",
    "PatterConnectionError",
    "AuthenticationError",
    "ProvisionError",
    "RateLimitError",
    "SentenceChunker",
    "PipelineHookExecutor",
    "filter_markdown",
    "filter_emoji",
    "filter_for_tts",
    "tool",
    "Tool",
    "guardrail",
    "Twilio",
    "Telnyx",
    "OpenAIRealtime",
    "ElevenLabsConvAI",
    "DeepgramSTT",
    "WhisperSTT",
    "OpenAITranscribeSTT",
    "CartesiaSTT",
    "SonioxSTT",
    "SpeechmaticsSTT",
    "AssemblyAISTT",
    "ElevenLabsTTS",
    "ElevenLabsWebSocketTTS",
    "OpenAITTS",
    "CartesiaTTS",
    "RimeTTS",
    "LMNTTTS",
    "OpenAILLM",
    "AnthropicLLM",
    "GroqLLM",
    "CerebrasLLM",
    "GoogleLLM",
    "TwilioAdapter",
    "TelnyxAdapter",
    "SileroVAD",
    "init_tracing",
    "start_span",
    "SPAN_CALL",
    "SPAN_STT",
    "SPAN_LLM",
    "SPAN_TTS",
    "SPAN_TOOL",
    "SPAN_ENDPOINT",
    "SPAN_BARGEIN",
    "CloudflareTunnel",
    "Ngrok",
    "Static",
    "StaticTunnel",
    "FallbackLLMProvider",
    "AllProvidersFailedError",
    "PartialStreamError",
    "ChatContext",
    "ChatMessage",
    "IVRActivity",
    "TfidfLoopDetector",
    "DtmfEvent",
    "format_dtmf",
    "ScheduleHandle",
    "schedule_cron",
    "schedule_once",
    "schedule_interval",
    "mix_pcm",
    # Dashboard / metrics surface (parity with TS).
    "MetricsStore",
    "mount_dashboard",
    "mount_api",
    "notify_dashboard",
    "make_auth_dependency",
    "make_auth_middleware",
    "calls_to_csv",
    "calls_to_json",
    # LLM loop primitives.
    "LLMLoop",
    "LLMProvider",
    "OpenAILLMProvider",
    "LLMChunk",
    "DefaultToolExecutor",
    # Remote message + test session.
    "RemoteMessageHandler",
    "is_remote_url",
    "is_websocket_url",
    "TestSession",
    # Background audio.
    "BackgroundAudioPlayer",
    "BuiltinAudioClip",
    "builtin_clip_path",
    "select_sound_from_list",
    # Transcoding.
    "PcmCarry",
    "StatefulResampler",
    "create_resampler_8k_to_16k",
    "create_resampler_16k_to_8k",
    "create_resampler_24k_to_16k",
    "create_resampler_24k_to_8k",
    "mulaw_to_pcm16",
    "pcm16_to_mulaw",
    "resample_8k_to_16k",
    "resample_16k_to_8k",
    "resample_24k_to_16k",
    # Pricing.
    "DEFAULT_PRICING",
    "merge_pricing",
    "calculate_stt_cost",
    "calculate_tts_cost",
    "calculate_realtime_cost",
    "calculate_telephony_cost",
    # Per-call metrics.
    "CallMetricsAccumulator",
    # Observability extras.
    "is_tracing_enabled",
    "EventBus",
    "PatterEventType",
    # External-agent integrations.
    "PatterTool",
    "PatterToolResult",
]
