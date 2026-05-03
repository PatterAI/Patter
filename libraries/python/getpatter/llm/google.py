"""Google Gemini LLM for Patter pipeline mode."""

from __future__ import annotations

import os
from typing import ClassVar

from getpatter.providers.google_llm import GoogleLLMProvider as _GoogleLLM

__all__ = ["LLM"]


class LLM(_GoogleLLM):
    """Google Gemini LLM provider (``google-genai`` SDK).

    Example::

        from getpatter.llm import google

        llm = google.LLM()                            # reads GEMINI_API_KEY or GOOGLE_API_KEY
        llm = google.LLM(api_key="AIza...", model="gemini-2.5-flash")
    """

    provider_key: ClassVar[str] = "google"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = "gemini-2.5-flash",
        **kwargs,
    ) -> None:
        # Prefer ``GEMINI_API_KEY`` (more specific), fall back to
        # ``GOOGLE_API_KEY`` for parity with the underlying provider adapter.
        key = (
            api_key
            or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
        )
        if not key and not kwargs.get("vertexai"):
            raise ValueError(
                "Google Gemini LLM requires an api_key. Pass api_key='AIza...' or "
                "set GEMINI_API_KEY (or GOOGLE_API_KEY) in the environment."
            )
        super().__init__(api_key=key, model=model, **kwargs)
