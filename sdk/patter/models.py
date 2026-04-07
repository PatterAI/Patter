from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Guardrail:
    """Output guardrail — filters AI responses before TTS.

    Args:
        name: Identifier used in log messages when the guardrail fires.
        check: Optional callable ``(text: str) -> bool`` that returns ``True``
            when the response should be blocked.
        blocked_terms: Optional list of words/phrases; any match blocks the
            response (case-insensitive substring check).
        replacement: What the agent says instead when a response is blocked.
    """

    name: str
    check: object = None  # Callable[[str], bool] | None
    blocked_terms: list | None = None
    replacement: str = "I'm sorry, I can't respond to that."


@dataclass(frozen=True)
class Agent:
    """Configuration for a local-mode voice AI agent."""

    system_prompt: str
    voice: str = "alloy"
    model: str = "gpt-4o-mini-realtime-preview"
    language: str = "en"
    first_message: str = ""
    tools: list[dict] | None = None
    provider: str = "openai_realtime"  # "openai_realtime" | "elevenlabs_convai" | "pipeline"
    stt: STTConfig | None = None  # which STT provider to use in pipeline mode
    tts: TTSConfig | None = None  # which TTS provider to use in pipeline mode
    variables: dict | None = None  # Dynamic variables for ``{placeholder}`` substitution in system_prompt
    guardrails: list | None = None  # List of Guardrail objects or guardrail dicts


@dataclass(frozen=True)
class CallEvent:
    """Call lifecycle event."""
    call_id: str
    caller: str = ""
    callee: str = ""
    direction: str = ""


@dataclass(frozen=True)
class IncomingMessage:
    text: str
    call_id: str
    caller: str


@dataclass(frozen=True)
class STTConfig:
    provider: str
    api_key: str
    language: str = "en"

    def to_dict(self) -> dict:
        return {"provider": self.provider, "api_key": self.api_key, "language": self.language}


@dataclass(frozen=True)
class TTSConfig:
    provider: str
    api_key: str
    voice: str = "alloy"

    def to_dict(self) -> dict:
        return {"provider": self.provider, "api_key": self.api_key, "voice": self.voice}
