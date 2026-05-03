"""ElevenLabs Conversational AI engine marker for Patter."""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = ["ConvAI"]


@dataclass(frozen=True)
class ConvAI:
    """ElevenLabs Conversational AI engine config.

    Holds the minimal settings needed by the Patter server to instantiate
    :class:`getpatter.providers.elevenlabs_convai.ElevenLabsConvAIAdapter`.

    Example::

        from getpatter.engines import elevenlabs

        engine = elevenlabs.ConvAI()                         # reads env vars
        engine = elevenlabs.ConvAI(api_key="...", agent_id="ag_...", voice="...")
    """

    api_key: str = ""
    agent_id: str = ""
    voice: str = ""

    def __post_init__(self) -> None:
        key = self.api_key or os.environ.get("ELEVENLABS_API_KEY", "")
        agent = self.agent_id or os.environ.get("ELEVENLABS_AGENT_ID", "")
        if not key:
            raise ValueError(
                "ElevenLabs ConvAI engine requires an api_key. Pass "
                "api_key='...' or set ELEVENLABS_API_KEY in the environment."
            )
        if not agent:
            raise ValueError(
                "ElevenLabs ConvAI engine requires an agent_id. Create one "
                "in the ElevenLabs dashboard "
                "(https://elevenlabs.io/app/conversational-ai) — the agent "
                "ID is per-deployed-agent and cannot be derived from the "
                "API key alone. Then either pass agent_id='ag_...' at "
                "construction or set ELEVENLABS_AGENT_ID in the environment."
            )
        object.__setattr__(self, "api_key", key)
        object.__setattr__(self, "agent_id", agent)

    @property
    def kind(self) -> str:
        """Stable discriminator used for Phase 2 dispatch."""
        return "elevenlabs_convai"
