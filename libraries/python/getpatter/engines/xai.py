"""xAI Grok Voice Agent engine marker for Patter (Beta).

Wraps the xAI Voice Agent API (OpenAI-Realtime-GA-compatible). The client
dispatches to
:class:`getpatter.providers.xai_realtime.XaiRealtimeAdapter` when this marker is
passed to ``Patter.agent(engine=...)``.

Beta: validated against the xAI Voice Agent API spec; not yet exercised against
a live phone call.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = ["XaiRealtime"]


@dataclass(frozen=True)
class XaiRealtime:
    """xAI Grok Voice Agent engine config — selects ``grok-voice-latest``.

    Holds the settings the Patter server needs to instantiate
    :class:`getpatter.providers.xai_realtime.XaiRealtimeAdapter` at call time.
    All fields are optional with safe defaults; the xAI-specific tuning knobs
    are omitted from ``session.update`` unless set.

    Example::

        from getpatter import Patter, Twilio, XaiRealtime

        phone = Patter(carrier=Twilio(), phone_number="+1...")
        agent = phone.agent(
            engine=XaiRealtime(voice="eve", reasoning_effort="none"),
            system_prompt="You are a friendly receptionist.",
            first_message="Hello! How can I help?",
        )
    """

    api_key: str = ""
    voice: str = "eve"
    model: str = "grok-voice-latest"
    # Reasoning-effort tier. ``None`` leaves the field unset (server default
    # ``"high"``). xAI accepts ``"high"`` or ``"none"`` (disable reasoning for
    # lower latency on simple flows).
    reasoning_effort: Literal["high", "none"] | None = None
    # VAD activation threshold (0.1–0.9). ``None`` keeps the xAI default (0.85).
    vad_threshold: float | None = None
    # Silence (ms, 0–10000) required before the server ends the user's turn.
    # ``None`` keeps the adapter default.
    silence_duration_ms: int | None = None
    # Audio (ms, 0–10000) captured before detected speech start. ``None`` keeps
    # the xAI default (333).
    prefix_padding_ms: int | None = None
    # When set, the server re-engages the user after this many ms of silence
    # following an assistant turn. ``None`` disables the idle timer.
    idle_timeout_ms: int | None = None
    # BCP-47 language hint biasing ASR transcription (e.g. ``"es-MX"``). ``None``
    # keeps automatic language detection.
    language_hint: str | None = None
    # Bias terms (≤100, ≤50 chars each) for ASR transcription.
    keyterms: tuple[str, ...] = ()
    # Assistant audio playback speed multiplier (0.7–1.5). ``None`` keeps 1.0.
    speed: float | None = None
    # Pronunciation replacements applied before TTS (e.g.
    # ``{"Acme Mobile": "Acme Mobull"}``). ``None`` disables them.
    replace: dict[str, str] | None = None
    # Opt in to Session Resumption (cache + replay conversation turns on
    # reconnect). ``False`` (default) keeps the stateless behaviour.
    resumption: bool = False
    # xAI server-side tools appended verbatim to ``session.tools`` (e.g.
    # ``({"type": "web_search"},)``); xAI executes these on its side.
    server_tools: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        key = self.api_key or os.environ.get("XAI_API_KEY", "")
        if not key:
            raise ValueError(
                "xAI Realtime engine requires an api_key. Pass "
                "api_key='xai-...' or set XAI_API_KEY in the environment."
            )
        object.__setattr__(self, "api_key", key)

    @property
    def kind(self) -> str:
        """Stable discriminator used for engine dispatch."""
        return "xai_realtime"
