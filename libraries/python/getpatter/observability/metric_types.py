"""Typed metric data models for Patter pipeline events.

All models are frozen dataclasses (no pydantic dependency) and form Patter's
canonical observability surface — emitted by :mod:`getpatter.services.metrics`
and consumed by the dashboard, EventBus subscribers, and exporters.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Metadata:
    """Provider / model metadata attached to a metric."""

    model_name: str | None = None
    model_provider: str | None = None


@dataclass(frozen=True)
class CachedTokenDetails:
    """Breakdown of cached tokens by modality."""

    audio_tokens: int = 0
    text_tokens: int = 0
    image_tokens: int = 0


@dataclass(frozen=True)
class InputTokenDetails:
    """Detailed input token breakdown."""

    audio_tokens: int = 0
    text_tokens: int = 0
    image_tokens: int = 0
    cached_tokens: int = 0
    cached_tokens_details: CachedTokenDetails | None = None


@dataclass(frozen=True)
class OutputTokenDetails:
    """Detailed output token breakdown."""

    text_tokens: int = 0
    audio_tokens: int = 0
    image_tokens: int = 0


@dataclass(frozen=True)
class LLMUsage:
    """LLM token usage for a single response."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    prompt_cached_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0


@dataclass(frozen=True)
class RealtimeUsage:
    """Token usage for an OpenAI Realtime session."""

    session_duration_seconds: float = 0.0
    tokens_per_second: float = 0.0
    input_token_details: InputTokenDetails = field(default_factory=InputTokenDetails)
    output_token_details: OutputTokenDetails = field(default_factory=OutputTokenDetails)
    metadata: Metadata | None = None


@dataclass(frozen=True)
class EOUMetrics:
    """End-of-utterance timing metrics.

    All delay fields are in **seconds**. Captures the timing relationship
    between VAD stop, STT final transcript, and the moment the pipeline
    commits the turn for LLM processing.
    """

    end_of_utterance_delay: float
    transcription_delay: float
    on_user_turn_completed_delay: float
    timestamp: float = field(default_factory=time.time)
    speech_id: str | None = None
    metadata: Metadata | None = None


@dataclass(frozen=True)
class InterruptionMetrics:
    """Barge-in / overlap metrics — heuristic only, no ML classification.

    All duration fields are in **seconds**.
    """

    total_duration: float
    detection_delay: float
    num_interruptions: int
    num_backchannels: int
    timestamp: float = field(default_factory=time.time)
    prediction_duration: float = 0.0
    metadata: Metadata | None = None


@dataclass(frozen=True)
class TTFBMetrics:
    """Time-to-first-byte latency for a pipeline stage."""

    processor: str
    value: float
    timestamp: float = field(default_factory=time.time)
    model: str | None = None


@dataclass(frozen=True)
class ProcessingMetrics:
    """Total processing time for a pipeline stage."""

    processor: str
    value: float
    timestamp: float = field(default_factory=time.time)
    model: str | None = None
