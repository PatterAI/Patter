"""Namespaced streaming TTS adapters for Patter pipeline mode.

Each submodule exposes a thin ``TTS`` subclass of the corresponding provider
adapter with an environment-variable fallback on ``api_key``.

Usage::

    from patter.tts import elevenlabs
    tts = elevenlabs.TTS(voice_id="...")   # reads ELEVENLABS_API_KEY
"""

from __future__ import annotations

__all__ = [
    "elevenlabs",
    "openai",
    "cartesia",
    "rime",
    "lmnt",
]
