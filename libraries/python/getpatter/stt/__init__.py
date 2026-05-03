"""Namespaced streaming STT adapters for Patter pipeline mode.

Each submodule exposes a thin ``STT`` subclass of the corresponding provider
adapter with:

* An environment-variable fallback on ``api_key``.
* A clear error message when credentials are missing.

Usage::

    from getpatter.stt import deepgram
    stt = deepgram.STT()  # reads DEEPGRAM_API_KEY
"""

from __future__ import annotations

__all__ = [
    "deepgram",
    "whisper",
    "cartesia",
    "soniox",
    "speechmatics",
    "assemblyai",
]
