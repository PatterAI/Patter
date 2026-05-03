"""Audio primitives — transcoding, PCM mixing, background audio playback.

Public symbols are re-exported from :mod:`getpatter` (top level). Direct
submodule imports remain stable: ``getpatter.audio.transcoding``,
``getpatter.audio.pcm_mixer``, ``getpatter.audio.background_audio``.
"""

from __future__ import annotations

__all__ = ["transcoding", "pcm_mixer", "background_audio"]
