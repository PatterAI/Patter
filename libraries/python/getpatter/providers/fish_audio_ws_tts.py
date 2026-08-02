"""WebSocket-based Fish Audio TTS provider — opt-in low-latency variant.

Beta: validated against the Fish Audio API spec (docs.fish.audio); not yet
exercised against a live phone call.

This adapter targets the streaming endpoint ``wss://api.fish.audio/v1/tts/live``
instead of the HTTP ``/v1/tts`` endpoint used by :class:`FishAudioTTS`. The
WebSocket path skips the per-utterance HTTP request setup (~50 ms) and pairs
with ``s2-pro``'s ~100 ms time-to-first-audio.

Usage matches :class:`FishAudioTTS` exactly::

    from getpatter.providers.fish_audio_ws_tts import FishAudioWebSocketTTS

    tts = FishAudioWebSocketTTS.for_twilio(api_key=...)
    async for audio in tts.synthesize("Hello world"):
        ...

Behaviour notes
---------------
* **Model support is narrower than HTTP.** Fish restricts the ``model:`` header
  on this endpoint to ``s1`` and ``s2-pro``; ``s2.1-pro`` is HTTP-only. The
  constructor rejects unsupported models with a message pointing at
  :class:`FishAudioTTS` rather than letting the socket fail at call time.
* Frames are **MessagePack**, not JSON — the server returns raw audio bytes
  inline, which JSON cannot carry without base64. This is the only reason the
  ``[fish_audio]`` extra pulls in a msgpack codec; the HTTP and ASR adapters
  work without it.
* The socket is opened **per-utterance**, matching the HTTP semantics and the
  ElevenLabs WebSocket adapter.
* Protocol: ``start`` (config) -> ``text`` (payload) -> ``flush`` -> ``stop``,
  with the server replying ``audio`` frames followed by ``finish``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Callable, ClassVar, Optional, Sequence, Union

try:
    import websockets
except ImportError as e:  # pragma: no cover - websockets is in main deps
    raise ImportError(
        "websockets is required for FishAudioWebSocketTTS — it is already "
        "a runtime dependency of getpatter, so this should never happen."
    ) from e

from getpatter.providers.fish_audio_tts import (
    FISH_AUDIO_DEFAULT_SAMPLE_RATE,
    FishAudioFormat,
    FishAudioLatency,
    FishAudioModel,
    FishAudioTTS,
)

logger = logging.getLogger("getpatter.providers.fish_audio_ws_tts")

FISH_AUDIO_WS_URL = "wss://api.fish.audio/v1/tts/live"

# Models Fish accepts on the ``/v1/tts/live`` socket. ``s2.1-pro`` and
# ``s2.1-pro-free`` are HTTP-only.
WS_SUPPORTED_MODELS: frozenset[str] = frozenset(
    {FishAudioModel.S1.value, FishAudioModel.S2_PRO.value}
)

# Connect timeout — 5 s keeps the carrier WebSocket from sitting in dead air
# while a stuck DNS / TLS handshake is retried.
DEFAULT_OPEN_TIMEOUT = 5.0

# Per-frame receive timeout. If the server stalls (no audio, no finish, no
# close) the generator would otherwise hang until carrier hangup.
DEFAULT_FRAME_TIMEOUT = 30.0

# Maximum size of a single audio frame from the server. Real frames are far
# smaller; anything beyond 1 MB is almost certainly malformed or hostile.
MAX_AUDIO_FRAME_SIZE = 1024 * 1024


class FishAudioWSError(Exception):
    """Raised when the Fish Audio WebSocket reports a server-side error."""


def _load_msgpack() -> tuple[Callable[[Any], bytes], Callable[[bytes], Any]]:
    """Resolve a MessagePack codec, preferring ``ormsgpack``.

    Returns a ``(packb, unpackb)`` pair. ``ormsgpack`` is what Fish's own
    Python SDK uses; plain ``msgpack`` is accepted as a fallback so users who
    already have it installed don't need a second codec.
    """
    try:  # pragma: no cover - import-path branch
        import ormsgpack

        return ormsgpack.packb, ormsgpack.unpackb
    except ImportError:
        pass
    try:  # pragma: no cover - import-path branch
        import msgpack

        return (
            lambda obj: msgpack.packb(obj, use_bin_type=True),
            lambda data: msgpack.unpackb(data, raw=False),
        )
    except ImportError as exc:
        raise ImportError(
            "FishAudioWebSocketTTS needs a MessagePack codec — the Fish "
            "streaming socket frames are msgpack, not JSON. Install with: "
            "pip install getpatter[fish_audio]  (or: pip install ormsgpack). "
            "The HTTP FishAudioTTS adapter has no such requirement."
        ) from exc


class FishAudioWebSocketTTS(FishAudioTTS):
    """Fish Audio TTS over the ``/v1/tts/live`` WebSocket (Beta).

    Same constructor surface as :class:`FishAudioTTS`, except the default model
    is ``s2-pro`` (the fastest model this endpoint serves) and ``s2.1-pro`` is
    rejected up-front because Fish does not route it here.
    """

    #: Stable pricing/dashboard key — read by stream-handler/metrics. Shares the
    #: HTTP adapter's key: same models, same per-byte rate.
    provider_key: ClassVar[str] = "fish_audio"

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        model: Union[FishAudioModel, str] = FishAudioModel.S2_PRO,
        voice: Optional[Union[str, Sequence[str]]] = None,
        format: Union[FishAudioFormat, str] = FishAudioFormat.PCM,
        sample_rate: int = FISH_AUDIO_DEFAULT_SAMPLE_RATE,
        latency: Union[FishAudioLatency, str] = FishAudioLatency.BALANCED,
        ws_url: str = FISH_AUDIO_WS_URL,
        open_timeout: float = DEFAULT_OPEN_TIMEOUT,
        frame_timeout: float = DEFAULT_FRAME_TIMEOUT,
        **kwargs: Any,
    ) -> None:
        if str(model) not in WS_SUPPORTED_MODELS:
            raise ValueError(
                f"FishAudioWebSocketTTS: model {str(model)!r} is not available on "
                f"the /v1/tts/live socket (Fish accepts only "
                f"{sorted(WS_SUPPORTED_MODELS)} there). Use the HTTP adapter "
                f"instead: FishAudioTTS(model={str(model)!r})."
            )
        super().__init__(
            api_key=api_key,
            model=model,
            voice=voice,
            format=format,
            sample_rate=sample_rate,
            latency=latency,
            **kwargs,
        )
        self.ws_url = ws_url
        self.open_timeout = open_timeout
        self.frame_timeout = frame_timeout
        # Resolved lazily on first synthesise so importing the module (and
        # constructing the adapter) never requires the msgpack extra.
        self._packb: Optional[Callable[[Any], bytes]] = None
        self._unpackb: Optional[Callable[[bytes], Any]] = None

    def __repr__(self) -> str:
        return (
            f"FishAudioWebSocketTTS(model={self.model!r}, voice={self.voice!r}, "
            f"format={self.format!r}, sample_rate={self.sample_rate})"
        )

    def _codec(self) -> tuple[Callable[[Any], bytes], Callable[[bytes], Any]]:
        if self._packb is None or self._unpackb is None:
            self._packb, self._unpackb = _load_msgpack()
        return self._packb, self._unpackb

    def _build_start_event(self) -> dict[str, Any]:
        """Build the ``start`` frame.

        The ``request`` object takes the same fields as the HTTP TTS body with
        an empty ``text`` — the actual text arrives in later ``text`` frames.
        """
        request = self._build_payload("")
        return {"event": "start", "request": request}

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Stream audio bytes for ``text`` over a per-utterance WebSocket.

        With the default ``format="pcm"`` these are raw PCM-16-LE chunks at the
        configured ``sample_rate``.
        """
        packb, unpackb = self._codec()
        self._record_synthesis_cost(text)

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "model": str(self.model),
        }

        ws = await asyncio.wait_for(
            websockets.connect(self.ws_url, additional_headers=headers),
            timeout=self.open_timeout,
        )
        try:
            await ws.send(packb(self._build_start_event()))
            await ws.send(packb({"event": "text", "text": text}))
            await ws.send(packb({"event": "flush"}))
            await ws.send(packb({"event": "stop"}))

            while True:
                try:
                    frame = await asyncio.wait_for(
                        ws.recv(), timeout=self.frame_timeout
                    )
                except asyncio.TimeoutError as exc:
                    raise FishAudioWSError(
                        f"Fish Audio WS stalled: no frame within {self.frame_timeout}s"
                    ) from exc
                except websockets.exceptions.ConnectionClosedOK:
                    break
                except websockets.exceptions.ConnectionClosed as exc:
                    raise FishAudioWSError(
                        f"Fish Audio WS closed unexpectedly: {exc}"
                    ) from exc

                audio, done = _decode_server_frame(frame, unpackb)
                if audio:
                    yield audio
                if done:
                    break
        finally:
            await ws.close()

    async def close(self) -> None:
        """Release the warmup HTTP session (the synthesis socket is
        per-utterance and closed by :meth:`synthesize`)."""
        await super().close()


def _decode_server_frame(
    frame: Union[bytes, str], unpackb: Callable[[bytes], Any]
) -> tuple[Optional[bytes], bool]:
    """Decode one server frame into ``(audio_bytes, is_final)``.

    Returns ``(None, False)`` for frames that carry neither audio nor a
    terminal signal (``log`` frames, unknown future event types) so the caller
    can keep reading. Raises :class:`FishAudioWSError` when the server reports
    ``finish`` with ``reason="error"``.
    """
    if isinstance(frame, str):
        # The protocol is msgpack-binary; a text frame is only ever an
        # out-of-band server message. Log it and keep going.
        logger.debug("Fish Audio WS text frame ignored: %s", frame[:200])
        return None, False

    try:
        parsed = unpackb(frame)
    except Exception as exc:  # noqa: BLE001 - malformed frame from the server
        raise FishAudioWSError(
            f"Fish Audio WS sent an undecodable frame: {exc}"
        ) from exc

    if not isinstance(parsed, dict):
        return None, False

    event = parsed.get("event")
    if event == "audio":
        audio = parsed.get("audio")
        if not isinstance(audio, (bytes, bytearray)):
            return None, False
        if len(audio) > MAX_AUDIO_FRAME_SIZE:
            raise FishAudioWSError(
                f"Fish Audio WS audio frame is {len(audio)} bytes, over the "
                f"{MAX_AUDIO_FRAME_SIZE}-byte sanity limit"
            )
        return bytes(audio), False
    if event == "finish":
        reason = parsed.get("reason")
        if reason == "error":
            raise FishAudioWSError(
                f"Fish Audio WS finished with an error: {parsed.get('message') or reason}"
            )
        return None, True
    if event == "log":
        logger.debug("Fish Audio WS log: %s", str(parsed.get("message"))[:200])
    return None, False
