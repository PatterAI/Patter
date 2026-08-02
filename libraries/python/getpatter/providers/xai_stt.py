"""xAI Speech-to-Text for the Patter SDK — streaming adapter + batch helper.

Beta: validated against the xAI STT API spec (docs.x.ai); not yet exercised
against a live phone call.

Two entry points live here:

* :class:`XaiSTT` — the streaming :class:`~getpatter.providers.base.STTProvider`
  used by Patter's pipeline mode. Connects to ``wss://api.x.ai/v1/stt`` (config
  passed entirely via URL query params — there is NO setup message), waits for
  the ``transcript.created`` handshake, then streams RAW BINARY audio frames in
  and yields :class:`~getpatter.providers.base.Transcript` events out. The
  ``is_final`` / ``speech_final`` two-flag scheme mirrors Deepgram's.
* :func:`transcribe` — a one-shot batch helper wrapping the REST
  ``POST https://api.x.ai/v1/stt`` endpoint (multipart, ``file`` sent LAST per
  the xAI requirement), returning a parsed :class:`XaiTranscription`.

Credential: ``XAI_API_KEY`` env var / ``api_key`` argument. Auth is an
``Authorization: Bearer <key>`` header on both endpoints.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, AsyncIterator, ClassVar, Optional, Sequence
from urllib.parse import urlencode

from getpatter.providers.base import STTProvider, Transcript

logger = logging.getLogger("getpatter")

# Lazy import: aiohttp is declared as an optional dep for this provider
# (shared ``[xai]`` extra with the xAI TTS adapter).
try:  # pragma: no cover - trivial import guard
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore

# === Constants ===

# Streaming (WebSocket) Speech-to-Text endpoint.
XAI_STT_WS_URL = "wss://api.x.ai/v1/stt"
# Batch (REST) Speech-to-Text endpoint.
XAI_STT_REST_URL = "https://api.x.ai/v1/stt"

# Streaming STT is billed at $0.20/hr; the batch REST path is cheaper at
# $0.10/hr. The pipeline meters the streaming rate (see ``pricing.py``); the
# batch constant below is documented for callers of :func:`transcribe`.
XAI_STT_BATCH_PRICE_PER_MINUTE = 0.10 / 60


class XaiSTTEncoding(StrEnum):
    """Raw audio encodings accepted by the xAI streaming ``encoding`` param."""

    PCM = "pcm"
    MULAW = "mulaw"
    ALAW = "alaw"


class XaiClientFrame(StrEnum):
    """xAI streaming STT client control-message ``type`` values."""

    FINALIZE = "finalize"
    AUDIO_DONE = "audio.done"


class XaiServerEvent(StrEnum):
    """xAI streaming STT server event ``type`` values."""

    TRANSCRIPT_CREATED = "transcript.created"
    TRANSCRIPT_PARTIAL = "transcript.partial"
    TRANSCRIPT_DONE = "transcript.done"
    ERROR = "error"


@dataclass(frozen=True)
class XaiWord:
    """A single word from an xAI transcription with timing (and speaker)."""

    text: str
    start: float
    end: float
    # Populated only when diarization is enabled; ``None`` otherwise.
    speaker: int | None = None


@dataclass(frozen=True)
class XaiTranscription:
    """Parsed result of a batch :func:`transcribe` call."""

    text: str
    language: str
    duration: float
    words: tuple[XaiWord, ...] = ()


def _parse_words(raw_words: Any) -> tuple[XaiWord, ...]:
    """Convert the raw ``words`` array from an xAI response into ``XaiWord``s."""
    if not isinstance(raw_words, list):
        return ()
    out: list[XaiWord] = []
    for w in raw_words:
        if not isinstance(w, dict):
            continue
        speaker = w.get("speaker")
        out.append(
            XaiWord(
                text=w.get("text", ""),
                start=float(w.get("start", 0.0)),
                end=float(w.get("end", 0.0)),
                speaker=int(speaker) if speaker is not None else None,
            )
        )
    return tuple(out)


class XaiSTT(STTProvider):
    """xAI streaming STT adapter (Beta).

    Connects to the xAI real-time Speech-to-Text WebSocket API and yields
    :class:`~getpatter.providers.base.Transcript` objects.

    The default ``encoding="pcm"`` / ``sample_rate=16000`` matches what
    Patter's pipeline feeds every STT adapter (the carrier bridge decodes
    inbound mu-law to linear16 @ 16 kHz before the STT, on Twilio and Telnyx
    alike), so this drops in without transcoding — mirroring
    :class:`~getpatter.providers.deepgram_stt.DeepgramSTT` /
    :class:`~getpatter.providers.soniox_stt.SonioxSTT`.

    Args:
        api_key: xAI API key. Falls back to ``XAI_API_KEY`` when omitted.
        language: Optional language code (e.g. ``"en"``, ``"it"``) — enables
            server-side text formatting (ITN) for that language.
        encoding: Raw audio encoding (``"pcm"`` | ``"mulaw"`` | ``"alaw"``).
        sample_rate: PCM sample rate (Hz).
        interim_results: Emit ``is_final=false`` partials (~every 500 ms).
        endpointing_ms: Silence (ms, 0–5000) before an utterance is marked
            final. ``None`` keeps the xAI default (10). ``0`` = any VAD
            boundary.
        smart_turn: End-of-turn confidence threshold (0.0–1.0) enabling xAI's
            Smart Turn ML end-of-turn model. ``None`` leaves it off.
        smart_turn_timeout_ms: Max silence (1–5000 ms) before forcing
            ``speech_final`` when ``smart_turn`` is set.
        keyterms: Bias terms (≤100, ≤50 chars each), sent as repeated
            ``keyterm=`` query params.
        diarize: Speaker diarization — words gain a ``speaker`` index.
        filler_words: Include "uh" / "um" in the transcript.
        base_url: Override the xAI STT WebSocket URL (used by tests).
    """

    #: Stable pricing/dashboard key — read by stream-handler/metrics.
    provider_key: ClassVar[str] = "xai"

    def __init__(
        self,
        api_key: Optional[str] = None,
        language: str | None = None,
        *,
        encoding: XaiSTTEncoding | str = XaiSTTEncoding.PCM,
        sample_rate: int = 16000,
        interim_results: bool = True,
        endpointing_ms: int | None = None,
        smart_turn: float | None = None,
        smart_turn_timeout_ms: int | None = None,
        keyterms: Sequence[str] = (),
        diarize: bool = False,
        filler_words: bool = False,
        base_url: str = XAI_STT_WS_URL,
    ) -> None:
        if aiohttp is None:
            raise ImportError(
                "aiohttp is required for XaiSTT. "
                "Install with: pip install getpatter[xai]"
            )
        resolved_key = api_key or os.environ.get("XAI_API_KEY")
        if not resolved_key:
            raise ValueError(
                "xAI STT requires an api_key. Pass api_key='...' or set "
                "XAI_API_KEY in the environment."
            )
        if smart_turn is not None and not (0.0 <= smart_turn <= 1.0):
            raise ValueError("smart_turn must be between 0.0 and 1.0")

        self.api_key = resolved_key
        self.language = language
        self.encoding = str(encoding)
        self.sample_rate = sample_rate
        self.interim_results = interim_results
        self.endpointing_ms = endpointing_ms
        self.smart_turn = smart_turn
        self.smart_turn_timeout_ms = smart_turn_timeout_ms
        self.keyterms = tuple(keyterms)
        self.diarize = diarize
        self.filler_words = filler_words
        self.base_url = base_url

        self._session: aiohttp.ClientSession | None = None
        self._owns_session: bool = False
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._audio_bytes_sent: int = 0

    def __repr__(self) -> str:
        return (
            f"XaiSTT(encoding={self.encoding!r}, sample_rate={self.sample_rate}, "
            f"smart_turn={self.smart_turn}, diarize={self.diarize})"
        )

    # ------------------------------------------------------------------
    # URL / query construction
    # ------------------------------------------------------------------

    def _build_query(self) -> list[tuple[str, str]]:
        """Build the ordered ``(key, value)`` query params for the WS URL.

        ``keyterm`` is repeatable (one pair per term), so the params are a
        list of tuples rather than a dict.
        """
        params: list[tuple[str, str]] = [
            ("sample_rate", str(self.sample_rate)),
            ("encoding", self.encoding),
            ("interim_results", "true" if self.interim_results else "false"),
        ]
        if self.language:
            params.append(("language", self.language))
        if self.endpointing_ms is not None:
            params.append(("endpointing", str(self.endpointing_ms)))
        if self.diarize:
            params.append(("diarize", "true"))
        if self.filler_words:
            params.append(("filler_words", "true"))
        if self.smart_turn is not None:
            params.append(("smart_turn", str(self.smart_turn)))
        if self.smart_turn_timeout_ms is not None:
            params.append(("smart_turn_timeout", str(self.smart_turn_timeout_ms)))
        for term in self.keyterms:
            params.append(("keyterm", term))
        return params

    def _build_url(self) -> str:
        return f"{self.base_url}?{urlencode(self._build_query())}"

    def _record_transcript_cost(self) -> None:
        """Emit ``patter.cost.stt_seconds`` for buffered audio."""
        try:
            from getpatter.observability.attributes import record_patter_attrs

            bytes_per_sample = 1 if self.encoding in ("mulaw", "alaw") else 2
            seconds = self._audio_bytes_sent / float(
                self.sample_rate * bytes_per_sample
            )
            record_patter_attrs(
                {
                    "patter.cost.stt_seconds": seconds,
                    "patter.stt.provider": "xai",
                }
            )
            self._audio_bytes_sent = 0
        except Exception:  # pragma: no cover — defense in depth
            logger.debug("_record_transcript_cost failed", exc_info=True)

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open the WebSocket and wait for the ``transcript.created`` handshake.

        Config is carried entirely in the URL query string — no setup message
        is sent. The server signals readiness with ``transcript.created``; we
        block on it so ``send_audio`` never races ahead of a ready session.
        """
        if self._ws is not None:
            return  # Already connected — idempotent.

        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._owns_session = True

        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            self._ws = await self._session.ws_connect(
                self._build_url(), headers=headers
            )
            await self._await_transcript_created()
        except Exception:
            if self._owns_session and self._session is not None:
                await self._session.close()
                self._session = None
                self._owns_session = False
            self._ws = None
            raise

    async def _await_transcript_created(self) -> None:
        """Read frames until ``transcript.created`` (raise on ``error``)."""
        assert self._ws is not None
        async for msg in self._ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except json.JSONDecodeError:
                continue
            event_type = data.get("type")
            if event_type == XaiServerEvent.TRANSCRIPT_CREATED.value:
                return
            if event_type == XaiServerEvent.ERROR.value:
                raise RuntimeError(
                    f"xAI STT setup error: {data.get('message', 'unknown')}"
                )
        raise RuntimeError("xAI STT closed before transcript.created handshake")

    async def send_audio(self, audio_chunk: bytes) -> None:
        """Send a raw audio chunk as a binary WebSocket frame (no base64)."""
        if self._ws is None:
            raise RuntimeError("XaiSTT is not connected. Call connect() first.")
        if not audio_chunk:
            return
        self._audio_bytes_sent += len(audio_chunk)
        await self._ws.send_bytes(audio_chunk)

    async def finalize(self) -> None:
        """Force the current utterance to ``speech_final`` immediately.

        The pipeline's VAD ``speech_end`` fast-path duck-types ``stt.finalize``
        — without it every turn waits out the full endpointing delay. xAI's WS
        API accepts ``{"type": "finalize"}`` for exactly this.
        """
        if self._ws is None or self._ws.closed:
            return
        try:
            await self._ws.send_str(json.dumps({"type": XaiClientFrame.FINALIZE.value}))
        except Exception as exc:  # noqa: BLE001 - best-effort fast path
            logger.debug("xAI STT finalize failed: %s", exc)

    # ------------------------------------------------------------------
    # Transcript stream
    # ------------------------------------------------------------------

    async def receive_transcripts(self) -> AsyncIterator[Transcript]:
        """Yield :class:`Transcript` objects from the xAI stream.

        Event mapping (mirrors Deepgram's is_final/speech_final scheme):
        - ``transcript.partial`` ``is_final=false`` → interim result.
        - ``transcript.partial`` ``is_final=true, speech_final=false`` → chunk
          final (~3 s locked).
        - ``transcript.partial`` ``is_final=true, speech_final=true`` →
          utterance final.
        - ``transcript.done`` → final flush, then the stream closes.
        - ``error`` → logged; the connection stays open.
        """
        if self._ws is None:
            raise RuntimeError("XaiSTT is not connected. Call connect() first.")

        async for msg in self._ws:
            if msg.type in (
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSING,
            ):
                break
            if msg.type == aiohttp.WSMsgType.ERROR:
                logger.error("XaiSTT WebSocket error: %s", self._ws.exception())
                break
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue

            try:
                data = json.loads(msg.data)
            except json.JSONDecodeError:
                logger.warning("XaiSTT: received non-JSON message")
                continue

            event_type = data.get("type")

            if event_type == XaiServerEvent.ERROR.value:
                # Errors do NOT close the connection — surface and keep going.
                logger.error("XaiSTT error: %s", data.get("message", "unknown"))
                continue

            if event_type == XaiServerEvent.TRANSCRIPT_PARTIAL.value:
                text = (data.get("text") or "").strip()
                if not text:
                    continue
                speech_final = bool(data.get("speech_final", False))
                is_final = bool(data.get("is_final", False) or speech_final)
                if is_final:
                    self._record_transcript_cost()
                yield Transcript(
                    text=text,
                    is_final=is_final,
                    speech_final=speech_final,
                    words=_forward_words(data.get("words")),
                )

            elif event_type == XaiServerEvent.TRANSCRIPT_DONE.value:
                text = (data.get("text") or "").strip()
                if text:
                    self._record_transcript_cost()
                    yield Transcript(
                        text=text,
                        is_final=True,
                        speech_final=True,
                        words=_forward_words(data.get("words")),
                    )
                break

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Signal end-of-audio, then close the WebSocket and owned resources."""
        if self._ws is not None:
            try:
                await self._ws.send_str(
                    json.dumps({"type": XaiClientFrame.AUDIO_DONE.value})
                )
            except Exception:  # noqa: BLE001
                pass
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None

        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None
            self._owns_session = False


def _forward_words(raw_words: Any) -> tuple[dict[str, Any], ...]:
    """Pass through the provider ``words`` array as a tuple of dicts."""
    if not isinstance(raw_words, list):
        return ()
    return tuple(w for w in raw_words if isinstance(w, dict))


# ---------------------------------------------------------------------------
# Batch transcription helper (REST)
# ---------------------------------------------------------------------------


def _build_transcribe_fields(
    *,
    audio: bytes | None,
    url: str | None,
    language: str | None,
    format: bool,
    diarize: bool,
    keyterms: Sequence[str],
    filler_words: bool,
    audio_format: str | None,
    sample_rate: int | None,
    filename: str,
) -> list[tuple[str, Any, dict[str, Any]]]:
    """Return the ordered multipart fields for :func:`transcribe`.

    xAI requires the ``file`` part to be LAST, so all scalar fields come first
    and the file (or ``url``) is appended at the end. Each entry is
    ``(name, value, extra_kwargs)`` where ``extra_kwargs`` feeds
    ``FormData.add_field`` (e.g. ``filename`` / ``content_type`` for the file).
    """
    fields: list[tuple[str, Any, dict[str, Any]]] = []
    if language is not None:
        fields.append(("language", language, {}))
    if format:
        fields.append(("format", "true", {}))
    if diarize:
        fields.append(("diarize", "true", {}))
    if filler_words:
        fields.append(("filler_words", "true", {}))
    if audio_format is not None:
        fields.append(("audio_format", audio_format, {}))
    if sample_rate is not None:
        fields.append(("sample_rate", str(sample_rate), {}))
    for term in keyterms:
        fields.append(("keyterm", term, {}))
    # ``file`` / ``url`` LAST — xAI requires the binary part to be the final
    # multipart field.
    if url is not None:
        fields.append(("url", url, {}))
    else:
        assert audio is not None
        fields.append(
            (
                "file",
                audio,
                {"filename": filename, "content_type": "application/octet-stream"},
            )
        )
    return fields


async def transcribe(
    audio: bytes | None = None,
    *,
    url: str | None = None,
    api_key: str | None = None,
    language: str | None = None,
    format: bool = False,  # noqa: A002 - matches the xAI API field name
    diarize: bool = False,
    keyterms: Sequence[str] = (),
    filler_words: bool = False,
    audio_format: str | None = None,
    sample_rate: int | None = None,
    filename: str = "audio.wav",
) -> XaiTranscription:
    """Transcribe an audio clip via the xAI batch REST endpoint (Beta).

    Exactly one of ``audio`` (raw bytes) or ``url`` (server-side download) is
    required. Container formats (WAV/MP3/…) are auto-detected; for raw
    headerless audio pass ``audio_format`` (``pcm`` | ``mulaw`` | ``alaw``) and
    ``sample_rate``.

    Args:
        audio: Audio file bytes (max 500 MB). Mutually exclusive with ``url``.
        url: URL for xAI to download and transcribe server-side.
        api_key: xAI API key. Falls back to ``XAI_API_KEY``.
        language: Language code; used with ``format=True`` for ITN formatting.
        format: Enable Inverse Text Normalization (requires ``language``).
        diarize: Speaker diarization — words gain a ``speaker`` index.
        keyterms: Bias terms (≤100, ≤50 chars each).
        filler_words: Include "uh" / "um".
        audio_format: Only for raw audio (``pcm`` | ``mulaw`` | ``alaw``).
        sample_rate: Only for raw audio (Hz).
        filename: Multipart filename for the uploaded ``file`` part.

    Returns:
        A parsed :class:`XaiTranscription`.
    """
    if aiohttp is None:
        raise ImportError(
            "aiohttp is required for xAI transcribe(). "
            "Install with: pip install getpatter[xai]"
        )
    if (audio is None) == (url is None):
        raise ValueError("transcribe() requires exactly one of audio= or url=")
    resolved_key = api_key or os.environ.get("XAI_API_KEY")
    if not resolved_key:
        raise ValueError(
            "xAI transcribe() requires an api_key. Pass api_key='...' or set "
            "XAI_API_KEY in the environment."
        )

    form = aiohttp.FormData()
    for name, value, extra in _build_transcribe_fields(
        audio=audio,
        url=url,
        language=language,
        format=format,
        diarize=diarize,
        keyterms=keyterms,
        filler_words=filler_words,
        audio_format=audio_format,
        sample_rate=sample_rate,
        filename=filename,
    ):
        form.add_field(name, value, **extra)

    headers = {"Authorization": f"Bearer {resolved_key}"}
    async with aiohttp.ClientSession() as session:
        async with session.post(
            XAI_STT_REST_URL,
            headers=headers,
            data=form,
            timeout=aiohttp.ClientTimeout(total=300),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"xAI STT error {resp.status}: {body[:500]}")
            payload = await resp.json()

    return XaiTranscription(
        text=payload.get("text", ""),
        language=payload.get("language", ""),
        duration=float(payload.get("duration", 0.0)),
        words=_parse_words(payload.get("words")),
    )
