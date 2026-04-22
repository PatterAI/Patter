"""End-to-end conversational engines for Patter.

Engines (OpenAI Realtime, ElevenLabs ConvAI) own the full STT+LLM+TTS loop
in a single WebSocket. Each submodule exposes a marker dataclass that Phase 2
dispatches on to instantiate the matching server-side adapter.

Usage::

    from getpatter.engines import openai
    engine = openai.Realtime(voice="alloy")   # reads OPENAI_API_KEY
"""

from __future__ import annotations

__all__ = ["openai", "elevenlabs"]
