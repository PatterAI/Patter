"""Gemini Live engine marker for Patter.

Selects Google's Gemini Live native-audio API (audio-in -> audio-out). The
client dispatches to
:class:`getpatter.providers.gemini_live.GeminiLiveAdapter` (through the
carrier-codec shim in :mod:`getpatter.providers.gemini_live_bridge`) when this
marker is passed to ``Patter.agent(engine=...)``.

Like the other engine markers this is a tiny immutable config object: it carries
credentials / voice / model / audio rates only. The runtime session lives in the
adapter, built server-side at call time.

Options present on the TypeScript ``GeminiLive`` marker but intentionally
ABSENT here, because the Python ``GeminiLiveAdapter`` has no constructor
argument that could honour them (forwarding them would be a silent no-op):

``affectiveDialog``, ``proactiveAudio``, ``vad``, ``thinking``,
``thinkingBudget``
    Native-audio / VAD / chain-of-thought knobs the TS adapter writes into its
    Live ``config``. The Python adapter builds its config without them.
``apiVersion``
    The Python adapter pins ``v1alpha`` (native-audio models are v1alpha-only)
    and exposes no override.

Add the matching adapter arguments first if these are needed; do not widen this
marker on its own.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from getpatter.providers.gemini_live import (
    DEFAULT_INPUT_SAMPLE_RATE_HZ,
    DEFAULT_OUTPUT_SAMPLE_RATE_HZ,
    GeminiLiveModel,
    GeminiLiveVoice,
)

__all__ = ["GeminiLive", "GEMINI_API_KEY_ENV_VARS"]

# Env vars checked, in order, when ``api_key`` is left empty. Mirrors
# ``getpatter.llm.google.LLM``: the Gemini-specific name wins over the broader
# Google one. Same pair (and order) as the TypeScript marker.
GEMINI_API_KEY_ENV_VARS: tuple[str, ...] = ("GEMINI_API_KEY", "GOOGLE_API_KEY")

# Patter's default Gemini Live model. Newer than the adapter's own default
# (which stays put for direct ``GeminiLiveAdapter`` users) and identical to the
# TypeScript engine default.
DEFAULT_MODEL = GeminiLiveModel.LIVE_3_1_FLASH_PREVIEW.value
DEFAULT_VOICE = GeminiLiveVoice.PUCK.value


@dataclass(frozen=True)
class GeminiLive:
    """Gemini Live engine config — selects Google's native-audio Live API.

    Holds the settings the Patter server needs to instantiate
    :class:`getpatter.providers.gemini_live.GeminiLiveAdapter` at call time.
    All fields are optional with safe defaults; knobs left at ``None`` are
    omitted so the adapter's own defaults stay authoritative.

    Example::

        from getpatter import Patter, Twilio, GeminiLive

        phone = Patter(carrier=Twilio(), phone_number="+1...")
        agent = phone.agent(
            engine=GeminiLive(voice="Puck"),
            system_prompt="You are a friendly receptionist.",
            first_message="Hello! How can I help?",
        )
    """

    # ``repr=False``: the marker is routinely printed in READMEs, docstrings
    # and error paths, and a default dataclass repr would put the raw key on
    # stdout / in a log line.
    api_key: str = field(default="", repr=False)
    model: str = DEFAULT_MODEL
    voice: str = DEFAULT_VOICE
    # BCP-47 language for the spoken reply. ``None`` defers to
    # ``Patter.agent(language=...)`` and then the adapter default (``en-US``).
    language: str | None = None
    # Sampling temperature. ``None`` keeps the adapter default (0.8).
    temperature: float | None = None
    # PCM16 mono rates Gemini negotiates on the Live channel. The carrier shim
    # resamples the 8 kHz telephony leg to/from these, so changing them changes
    # the resampling chain, not the wire format the carrier sees.
    input_sample_rate: int = DEFAULT_INPUT_SAMPLE_RATE_HZ
    output_sample_rate: int = DEFAULT_OUTPUT_SAMPLE_RATE_HZ

    def __post_init__(self) -> None:
        # Unlike the other engine markers this does NOT raise on a missing key:
        # ``GeminiLive()`` must stay constructible for introspection / docs on
        # a machine with no Google credentials. The call path fails fast
        # instead — ``Patter.agent()`` rejects a keyless Gemini Live agent at
        # build time, long before a call is dialled.
        if self.api_key:
            return
        for env_var in GEMINI_API_KEY_ENV_VARS:
            key = os.environ.get(env_var, "")
            if key:
                object.__setattr__(self, "api_key", key)
                return

    @property
    def kind(self) -> str:
        """Stable discriminator used for engine dispatch."""
        return "gemini_live"
