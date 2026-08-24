"""Inworld Realtime engine marker for Patter.

Selects Inworld's Realtime speech-to-speech API. Inworld advertises an "OpenAI
Realtime migration" path — the event schema, the session structure, and the
client/server events are compatible with OpenAI's Realtime API, so the session
is driven with the same ``session.update`` / ``response.create`` / streaming
delta wire shape. The runtime lives in
:class:`getpatter.providers.inworld_realtime.InworldRealtimeAdapter`, which
subclasses the v1 OpenAI Realtime adapter and only swaps the endpoint + auth.

Like the other engine markers this is a tiny immutable config object: it carries
credentials / voice / model only. The session is constructed at call time by the
realtime stream-handler.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from getpatter.providers.inworld_realtime import (
    INWORLD_REALTIME_DEFAULT_MODEL,
    INWORLD_REALTIME_DEFAULT_VOICE,
)

if TYPE_CHECKING:
    # Imported for the ``turn_detection`` forward-reference annotation only.
    # ``RealtimeTurnDetection`` validates itself in its own ``__post_init__``
    # (models.py), so the marker needs no construction-time validation.
    from getpatter.models import RealtimeTurnDetection

__all__ = ["InworldRealtime"]


@dataclass(frozen=True)
class InworldRealtime:
    """Inworld Realtime engine config — OpenAI-Realtime-compatible speech-to-speech.

    Holds the settings the Patter server needs to instantiate
    :class:`getpatter.providers.inworld_realtime.InworldRealtimeAdapter` at call
    time. All fields are optional with safe defaults.

    Example::

        from getpatter import Patter, Twilio, InworldRealtime

        phone = Patter(carrier=Twilio(), phone_number="+1...")
        agent = phone.agent(
            engine=InworldRealtime(voice="Ashley"),   # reads INWORLD_API_KEY
            system_prompt="You are a friendly receptionist.",
            first_message="Hello! How can I help?",
        )
    """

    #: Inworld Realtime API key (Bearer "Realtime key"). Falls back to the
    #: ``INWORLD_API_KEY`` env var when omitted.
    api_key: str = ""
    #: Voice name (e.g. ``"Ashley"``, ``"Olivia"``).
    voice: str = INWORLD_REALTIME_DEFAULT_VOICE
    #: Realtime model id, passed through as the ``?model=`` query param.
    #: Override with the exact model id from your Inworld dashboard.
    model: str = INWORLD_REALTIME_DEFAULT_MODEL
    # Override the WebSocket base URL (no query string). ``None`` keeps
    # ``wss://api.inworld.ai/v1/realtime``. Use this to point at an alternate /
    # on-prem deployment, or to supply a session-scoped URL if your Inworld
    # account requires the ``/v1/realtime/session?key=<session-id>`` flow.
    base_url: str | None = None
    # ISO-639-1 language hint for input transcription (e.g. ``"it"``, ``"en"``).
    # Pins the transcription model to one language instead of auto-detecting per
    # utterance. ``None`` (default) keeps auto-detect. Display-only.
    transcription_language: str | None = None
    # Turn-detection tuning. ``None`` (default) keeps the server VAD defaults.
    # Raise the threshold, or switch to ``semantic_vad`` with
    # ``eagerness="low"``, to stop speakerphone noise triggering false barge-ins.
    turn_detection: "RealtimeTurnDetection | None" = None
    # Gate the model's response on the input transcript (legacy behaviour).
    # ``None``/``False`` (default) — the model responds on speech-stop,
    # independent of the transcript. ``True`` restores transcript-gated
    # responses.
    gate_response_on_transcript: bool | None = None

    def __post_init__(self) -> None:
        key = self.api_key or os.environ.get("INWORLD_API_KEY", "")
        if not key:
            raise ValueError(
                "Inworld Realtime engine requires an api_key. Pass "
                "api_key='...' or set INWORLD_API_KEY in the environment."
            )
        object.__setattr__(self, "api_key", key)

    @property
    def kind(self) -> str:
        """Stable discriminator used for engine dispatch."""
        return "inworld_realtime"
