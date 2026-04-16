__version__ = "0.3.0"

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
from patter.services.tool_decorator import tool
from patter.services.fallback_provider import (
    FallbackLLMProvider,
    AllProvidersFailedError,
    PartialStreamError,
)
from patter.services.chat_context import ChatContext, ChatMessage
from patter.services.ivr import DtmfEvent, IVRActivity, TfidfLoopDetector, format_dtmf

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
    "FallbackLLMProvider",
    "AllProvidersFailedError",
    "PartialStreamError",
    "ChatContext",
    "ChatMessage",
    "IVRActivity",
    "TfidfLoopDetector",
    "DtmfEvent",
    "format_dtmf",
]
