"""Gemini Live engine marker for Patter.

Selects Google's Gemini Live native-audio API (audio-in → audio-out,
emotion-aware). Separate marker from the OpenAI / ElevenLabs engines because the
client dispatches to
:class:`getpatter.providers.gemini_live.GeminiLiveAdapter` when this marker is
passed to ``Patter.agent(engine=...)``.

Like the other engine markers this is a tiny immutable config object: it carries
credentials / voice / model only. The runtime session lives in
``GeminiLiveAdapter`` (constructed server-side at call time).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = ["GeminiLive"]


@dataclass(frozen=True)
class GeminiLive:
    """Gemini Live engine config — selects Google's native-audio Live API.

    Holds the minimal settings needed by the Patter server to instantiate
    :class:`getpatter.providers.gemini_live.GeminiLiveAdapter` at call time.

    The API key falls back to ``GEMINI_API_KEY`` then ``GOOGLE_API_KEY`` env
    vars when ``api_key`` is omitted (parity with TS ``GeminiLive``).

    Example::

        from getpatter import Patter, Twilio, GeminiLive

        phone = Patter(carrier=Twilio(), phone_number="+1...")
        agent = phone.agent(
            engine=GeminiLive(voice="Puck", model="gemini-3.1-flash-live-preview"),
            system_prompt="You are a friendly receptionist.",
            first_message="Hello! How can I help?",
        )
    """

    api_key: str = ""
    # ``None`` leaves model/voice/language/temperature unset so the adapter's
    # documented defaults apply (explicit kwargs on ``agent()`` still win).
    model: str | None = None
    voice: str | None = None
    language: str | None = None
    temperature: float | None = None

    def __post_init__(self) -> None:
        key = (
            self.api_key
            or os.environ.get("GEMINI_API_KEY", "")
            or os.environ.get("GOOGLE_API_KEY", "")
        )
        if not key:
            raise ValueError(
                "Gemini Live engine requires an api_key. Pass api_key='...' or "
                "set GEMINI_API_KEY (or GOOGLE_API_KEY) in the environment."
            )
        object.__setattr__(self, "api_key", key)

    @property
    def kind(self) -> str:
        """Stable discriminator used for engine dispatch."""
        return "gemini_live"
