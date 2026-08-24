"""Inworld Realtime WebSocket adapter — speech-to-speech over one socket.

Inworld's Realtime API is OpenAI-Realtime-compatible: Inworld documents an
"OpenAI Realtime migration" path where the event schema, the session structure,
and the client/server events match OpenAI's Realtime API, so migrating is "swap
the endpoint and the auth credentials". This adapter therefore SUBCLASSES
:class:`~getpatter.providers.openai_realtime.OpenAIRealtimeAdapter` (the v1
session shape) and overrides ONLY the transport:

* the WebSocket endpoint (``wss://api.inworld.ai/v1/realtime``, read by the
  inherited ``connect()`` / ``warmup()`` off ``OPENAI_REALTIME_URL``), and
* the ``provider_key`` plus the default model / voice.

Everything else — the ``session.created`` -> ``session.update`` ->
``session.updated`` handshake, the ``Authorization: Bearer <key>`` header, audio
delta dispatch, barge-in / truncate semantics, tool calling, ``send_text`` /
``send_first_message`` / ``send_reassurance`` — is inherited unchanged. Because
the class extends the v1 adapter, every feature gate the realtime stream-handler
applies to an OpenAI session fires for Inworld too, with no per-provider branch.

Audio: defaults to ``g711_ulaw`` pass-through (the Twilio / Telnyx / Plivo
carrier wire format), matching the v1 Realtime session shape Inworld mirrors. If
a given Inworld deployment only accepts PCM, pass ``audio_format="pcm16"``.

The exact production wire details (whether an Inworld account requires a
session-scoped ``?key=<session-id>`` obtained from a prior REST call, and which
audio formats are accepted) are not fully public. The defaults here are the
OpenAI-compatible ones; override ``base_url`` / ``audio_format`` per account.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from getpatter.providers.openai_realtime import OpenAIRealtimeAdapter

logger = logging.getLogger("getpatter.providers.inworld_realtime")

#: Default Inworld Realtime WebSocket base URL (no query string).
INWORLD_REALTIME_WS_URL = "wss://api.inworld.ai/v1/realtime"
#: Default model id — override with the exact id from the Inworld dashboard.
INWORLD_REALTIME_DEFAULT_MODEL = "inworld-realtime"
#: Default voice — an Inworld voice name (mirrors the Inworld TTS default).
INWORLD_REALTIME_DEFAULT_VOICE = "Ashley"


class InworldRealtimeAdapter(OpenAIRealtimeAdapter):
    """Realtime WebSocket adapter for Inworld's OpenAI-compatible Realtime API.

    Subclasses the v1 :class:`OpenAIRealtimeAdapter` and overrides the endpoint,
    the provider key, and the defaults. All runtime behaviour — handshake,
    audio, barge-in, tool dispatch — is inherited unchanged.
    """

    #: Inworld Realtime WebSocket endpoint (the inherited ``connect()`` reads it).
    OPENAI_REALTIME_URL: ClassVar[str] = INWORLD_REALTIME_WS_URL
    #: Stable pricing/dashboard key — matches the Inworld TTS provider key, the
    #: same choice the TypeScript adapter makes, because Inworld publishes no
    #: separate Realtime rate to meter against.
    provider_key: ClassVar[str] = "inworld"

    def __init__(self, *args: Any, base_url: str | None = None, **kwargs: Any) -> None:
        # Inworld defaults; only applied when the caller left the slot unset so
        # an explicit model / voice always wins.
        kwargs.setdefault("model", INWORLD_REALTIME_DEFAULT_MODEL)
        kwargs.setdefault("voice", INWORLD_REALTIME_DEFAULT_VOICE)
        super().__init__(*args, **kwargs)
        if base_url:
            # Shadow the class attribute per instance so the inherited
            # ``connect()`` targets an alternate / on-prem deployment. Trailing
            # slashes are trimmed because ``connect()`` appends ``?model=``.
            self.OPENAI_REALTIME_URL = base_url.rstrip("/")

    async def warmup(self) -> None:
        """No-op warmup.

        The inherited :meth:`OpenAIRealtimeAdapter.warmup` opens a socket and
        exchanges a full session handshake. Inworld is not wired into the
        prewarm pipeline (``Patter._park_provider_connections`` parks only the
        OpenAI realtime modes), so warming here would cost a connection with no
        consumer.
        """
        logger.debug("Inworld Realtime: warmup is a no-op (not parked)")

    async def open_parked_connection(self):  # type: ignore[no-untyped-def]
        """Parking is not supported for Inworld.

        Raises so any caller treats it as a cache miss and falls through to the
        cold :meth:`connect` path.
        """
        raise RuntimeError("Inworld Realtime: open_parked_connection is not supported")
