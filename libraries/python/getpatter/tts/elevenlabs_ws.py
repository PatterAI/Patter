"""ElevenLabs WebSocket TTS — backward-compatible alias.

As of 0.6.1, the canonical :class:`getpatter.tts.elevenlabs.TTS` facade
defaults to the WebSocket transport. This module re-exports it so existing
imports of the form ``from getpatter.tts.elevenlabs_ws import TTS`` keep
working without code changes.
"""

from __future__ import annotations

from getpatter.tts.elevenlabs import TTS

__all__ = ["TTS"]
