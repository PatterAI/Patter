"""Audio primitives — transcoding, PCM mixing, background audio, recording.

Public symbols are re-exported from :mod:`getpatter` (top level). Direct
submodule imports remain stable: ``getpatter.audio.transcoding``,
``getpatter.audio.pcm_mixer``, ``getpatter.audio.background_audio``,
``getpatter.audio.call_recorder``.
"""

from __future__ import annotations

__all__ = ["transcoding", "pcm_mixer", "background_audio", "call_recorder"]
