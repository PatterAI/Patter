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

__version__ = "0.4.4"

from patter.client import Patter
from patter.models import (
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
from patter.exceptions import PatterError, PatterConnectionError, AuthenticationError, ProvisionError
from patter.services.sentence_chunker import SentenceChunker
from patter.services.pipeline_hooks import PipelineHookExecutor
from patter.services.text_transforms import filter_markdown, filter_emoji, filter_for_tts

# New v0.5.0 public API (Phase 1a). ``tool`` here is the unified factory that
# supports both decorator use (``@tool`` on a typed function) and keyword
# construction (``tool(name=..., handler=...)``). It supersedes the historical
# :func:`patter.services.tool_decorator.tool` at the top level, but that module
# remains importable for users that already depend on the legacy dict shape.
from patter._public_api import Tool, tool, guardrail

# Flat aliases for the 4-line quickstart.
from patter.carriers.twilio import Carrier as Twilio
from patter.carriers.telnyx import Carrier as Telnyx
from patter.engines.openai import Realtime as OpenAIRealtime
from patter.engines.elevenlabs import ConvAI as ElevenLabsConvAI

# STT flat aliases — parity with sdk-ts/src/index.ts.
from patter.stt.deepgram import STT as DeepgramSTT
from patter.stt.whisper import STT as WhisperSTT
from patter.stt.cartesia import STT as CartesiaSTT
from patter.stt.soniox import STT as SonioxSTT
from patter.stt.speechmatics import STT as SpeechmaticsSTT
from patter.stt.assemblyai import STT as AssemblyAISTT

# TTS flat aliases.
from patter.tts.elevenlabs import TTS as ElevenLabsTTS
from patter.tts.openai import TTS as OpenAITTS
from patter.tts.cartesia import TTS as CartesiaTTS
from patter.tts.rime import TTS as RimeTTS
from patter.tts.lmnt import TTS as LMNTTTS

# Tunnel flat aliases.
from patter.tunnels import CloudflareTunnel, Ngrok, Static as StaticTunnel

from patter.services.fallback_provider import (
    FallbackLLMProvider,
    AllProvidersFailedError,
    PartialStreamError,
)
from patter.services.chat_context import ChatContext, ChatMessage
from patter.services.ivr import DtmfEvent, IVRActivity, TfidfLoopDetector, format_dtmf
from patter.scheduler import (
    ScheduleHandle,
    schedule_cron,
    schedule_once,
    schedule_interval,
)

# Top-level re-export for parity with TypeScript ``mixPcm`` (see BUG #04g).
# Import is lazy — `mix_pcm` triggers numpy import only on first call.
def mix_pcm(agent: bytes, bg: bytes, ratio: float) -> bytes:
    """Standalone PCM mixer — parity with TypeScript ``mixPcm(agent, bg, ratio)``."""
    from patter.services.pcm_mixer import mix_pcm as _mix_pcm
    return _mix_pcm(agent, bg, ratio)

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
    "PatterError",
    "PatterConnectionError",
    "AuthenticationError",
    "ProvisionError",
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
    "CartesiaSTT",
    "SonioxSTT",
    "SpeechmaticsSTT",
    "AssemblyAISTT",
    "ElevenLabsTTS",
    "OpenAITTS",
    "CartesiaTTS",
    "RimeTTS",
    "LMNTTTS",
    "CloudflareTunnel",
    "Ngrok",
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
]
