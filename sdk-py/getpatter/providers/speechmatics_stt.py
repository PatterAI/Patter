"""
Speechmatics Speech-to-Text adapter for the Patter SDK pipeline mode.

Uses the official ``speechmatics-voice[smart]`` SDK (imported lazily so the
dependency remains optional) to stream PCM audio to the Speechmatics real-time
API and yield :class:`~getpatter.providers.base.Transcript` events.

Adapted from LiveKit Agents (Apache 2.0):
https://github.com/livekit/agents
(source: livekit-plugins/livekit-plugins-speechmatics/livekit/plugins/speechmatics/stt.py
 at commit 78a66bcf79c5cea82989401c408f1dff4b961a5b)

Changes from upstream:
- Replaced ``stt.STT`` / ``stt.RecognizeStream`` hierarchy with Patter's
  :class:`~getpatter.providers.base.STTProvider`.
- Converted the dual-task (audio send / message receive) pipeline into a
  queue-fed AsyncIterator, matching the pattern in ``DeepgramSTT`` /
  ``WhisperSTT``.
- Dropped LiveKit-specific types (``SpeechEvent``, ``LanguageCode``,
  ``APIConnectOptions``), speaker-update side channels and diagnostics.
- Lazy-imports ``speechmatics.voice`` so consumers that do not install the
  ``speechmatics`` extra can still import the rest of the SDK.

Install with::

    pip install 'getpatter[speechmatics]'
"""

from __future__ import annotations

import asyncio
import logging
from enum import Enum
from typing import Any, AsyncIterator

from getpatter.providers.base import STTProvider, Transcript

logger = logging.getLogger("getpatter")


SPEECHMATICS_INSTALL_HINT = (
    "The Speechmatics Voice SDK is required for SpeechmaticsSTT. "
    "Install the optional dependency with:\n"
    "    pip install 'getpatter[speechmatics]'\n"
    "or directly: pip install 'speechmatics-voice[smart]>=0.2.8'"
)


class TurnDetectionMode(str, Enum):
    """Endpoint / turn-detection handling mode.

    Mirrors the values accepted by ``speechmatics.voice.VoiceAgentConfigPreset``.
    See LiveKit's original documentation for the semantic differences.
    """

    EXTERNAL = "external"
    FIXED = "fixed"
    ADAPTIVE = "adaptive"
    SMART_TURN = "smart_turn"


def _require_voice_sdk() -> Any:
    """Import and return the ``speechmatics.voice`` module.

    Raises a ``RuntimeError`` with installation instructions if the SDK is
    not available, rather than leaking an ``ImportError`` at import time.
    """
    try:
        import speechmatics.voice as voice_sdk  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - trivial guard
        raise RuntimeError(SPEECHMATICS_INSTALL_HINT) from exc
    return voice_sdk


class SpeechmaticsSTT(STTProvider):
    """Speechmatics real-time STT adapter.

    Connects via the official ``speechmatics-voice`` SDK and emits
    :class:`Transcript` objects for partial and final segments.

    Args:
        api_key: Speechmatics API key (or ``SPEECHMATICS_API_KEY`` env var).
        base_url: Optional override for the Speechmatics realtime endpoint
            (``SPEECHMATICS_RT_URL`` env var, falls back to the SDK default).
        language: BCP-47 language code (default ``"en"``).
        turn_detection_mode: How Speechmatics detects end of turn. Use
            ``TurnDetectionMode.EXTERNAL`` with an external VAD.
        sample_rate: PCM sample rate (Hz). Defaults to 16 kHz.
        enable_diarization: Attach speaker IDs to transcripts.
        max_delay: Max latency in seconds before the engine emits finals.
        end_of_utterance_silence_trigger: Silence (s) that triggers EOU.
        end_of_utterance_max_delay: Max EOU delay (s); must be greater
            than ``end_of_utterance_silence_trigger``.
        include_partials: Include partial words in interim output.
        additional_vocab: List of ``AdditionalVocabEntry`` from the SDK.
        operating_point: SDK ``OperatingPoint`` enum (accuracy vs latency).
        domain: Optional Speechmatics domain (e.g. ``"finance"``).
        output_locale: Optional output locale (e.g. ``"en-GB"``).
    """

    # Sentinel used to shut down the receive loop.
    _STOP = object()

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        language: str = "en",
        turn_detection_mode: TurnDetectionMode = TurnDetectionMode.ADAPTIVE,
        sample_rate: int = 16000,
        enable_diarization: bool = False,
        max_delay: float | None = None,
        end_of_utterance_silence_trigger: float | None = None,
        end_of_utterance_max_delay: float | None = None,
        include_partials: bool = True,
        additional_vocab: list[Any] | None = None,
        operating_point: Any | None = None,
        domain: str | None = None,
        output_locale: str | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("Speechmatics api_key is required")

        # Validate ranges (mirrors LiveKit's upstream checks).
        if end_of_utterance_silence_trigger is not None and not (
            0 < end_of_utterance_silence_trigger < 2
        ):
            raise ValueError(
                "end_of_utterance_silence_trigger must be between 0 and 2"
            )
        if (
            end_of_utterance_max_delay is not None
            and end_of_utterance_silence_trigger is not None
            and end_of_utterance_max_delay <= end_of_utterance_silence_trigger
        ):
            raise ValueError(
                "end_of_utterance_max_delay must be greater than "
                "end_of_utterance_silence_trigger"
            )
        if max_delay is not None and not (0.7 <= max_delay <= 4.0):
            raise ValueError("max_delay must be between 0.7 and 4.0")

        # Eagerly validate SDK availability so failures surface at init.
        self._voice = _require_voice_sdk()

        self.api_key = api_key
        self.base_url = base_url
        self.language = language
        self.turn_detection_mode = turn_detection_mode
        self.sample_rate = sample_rate
        self.enable_diarization = enable_diarization
        self.max_delay = max_delay
        self.end_of_utterance_silence_trigger = end_of_utterance_silence_trigger
        self.end_of_utterance_max_delay = end_of_utterance_max_delay
        self.include_partials = include_partials
        self.additional_vocab = additional_vocab or []
        self.operating_point = operating_point
        self.domain = domain
        self.output_locale = output_locale

        self._client: Any | None = None
        self._queue: asyncio.Queue[Any] = asyncio.Queue()

    def __repr__(self) -> str:
        return (
            f"SpeechmaticsSTT(language={self.language!r}, "
            f"turn_detection={self.turn_detection_mode.value!r}, "
            f"sample_rate={self.sample_rate})"
        )

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def _build_config(self) -> Any:
        """Build a ``VoiceAgentConfig`` from the adapter parameters."""
        voice = self._voice
        config = voice.VoiceAgentConfigPreset.load(self.turn_detection_mode.value)
        config.sample_rate = self.sample_rate
        config.audio_encoding = voice.AudioEncoding.PCM_S16LE
        config.language = self.language
        if self.domain is not None:
            config.domain = self.domain
        if self.output_locale is not None:
            config.output_locale = self.output_locale
        if self.additional_vocab:
            config.additional_vocab = self.additional_vocab
        if self.operating_point is not None:
            config.operating_point = self.operating_point
        if self.max_delay is not None:
            config.max_delay = self.max_delay
        if self.end_of_utterance_silence_trigger is not None:
            config.end_of_utterance_silence_trigger = (
                self.end_of_utterance_silence_trigger
            )
        if self.end_of_utterance_max_delay is not None:
            config.end_of_utterance_max_delay = self.end_of_utterance_max_delay
        config.enable_diarization = self.enable_diarization
        config.include_partials = self.include_partials
        return config

    def _register_handlers(self, client: Any) -> None:
        """Wire Speechmatics server events to our internal queue."""
        voice = self._voice

        def _enqueue(message: dict[str, Any]) -> None:
            self._queue.put_nowait(message)

        handlers = [
            voice.AgentServerMessageType.ADD_PARTIAL_SEGMENT,
            voice.AgentServerMessageType.ADD_SEGMENT,
            voice.AgentServerMessageType.END_OF_TURN,
            voice.AgentServerMessageType.ERROR,
            voice.AgentServerMessageType.WARNING,
        ]
        for event in handlers:
            client.on(event, _enqueue)

    async def connect(self) -> None:
        """Create the underlying ``VoiceAgentClient`` and connect."""
        if self._client is not None:
            return

        voice = self._voice
        kwargs: dict[str, Any] = {
            "api_key": self.api_key,
            "config": self._build_config(),
            "app": "patter-sdk",
        }
        if self.base_url:
            kwargs["url"] = self.base_url

        self._client = voice.VoiceAgentClient(**kwargs)
        self._register_handlers(self._client)
        await self._client.connect()

    async def send_audio(self, audio_chunk: bytes) -> None:
        """Send a PCM audio chunk (signed 16-bit little-endian)."""
        if self._client is None:
            raise RuntimeError(
                "SpeechmaticsSTT is not connected. Call connect() first."
            )
        if not audio_chunk:
            return
        await self._client.send_audio(audio_chunk)

    # ------------------------------------------------------------------
    # Transcript stream
    # ------------------------------------------------------------------

    def _message_to_transcript(self, message: dict[str, Any]) -> Transcript | None:
        """Translate a Speechmatics server message into a Patter Transcript."""
        voice = self._voice
        event = message.get("message")

        if event == voice.AgentServerMessageType.ADD_PARTIAL_SEGMENT:
            is_final = False
        elif event == voice.AgentServerMessageType.ADD_SEGMENT:
            is_final = True
        elif event == voice.AgentServerMessageType.END_OF_TURN:
            # End-of-turn carries no text; Patter signals finality via the
            # is_final flag on transcript events — ignore here.
            return None
        elif event in (
            voice.AgentServerMessageType.ERROR,
            voice.AgentServerMessageType.WARNING,
        ):
            logger.warning("SpeechmaticsSTT %s: %s", event, message)
            return None
        else:
            return None

        segments = message.get("segments") or []
        texts: list[str] = []
        confidences: list[float] = []
        for segment in segments:
            text = segment.get("text")
            if text:
                texts.append(text)
            confidence = segment.get("confidence")
            if isinstance(confidence, (int, float)):
                confidences.append(float(confidence))

        joined = " ".join(t for t in texts if t).strip()
        if not joined:
            return None

        confidence = (
            sum(confidences) / len(confidences) if confidences else 1.0
        )
        return Transcript(text=joined, is_final=is_final, confidence=confidence)

    async def receive_transcripts(self) -> AsyncIterator[Transcript]:
        """Yield :class:`Transcript` objects from Speechmatics events."""
        if self._client is None:
            raise RuntimeError(
                "SpeechmaticsSTT is not connected. Call connect() first."
            )

        while True:
            message = await self._queue.get()
            if message is self._STOP:
                break
            try:
                transcript = self._message_to_transcript(message)
            except Exception as exc:  # noqa: BLE001
                logger.exception("SpeechmaticsSTT handler error: %s", exc)
                continue
            if transcript is not None:
                yield transcript

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Disconnect the Speechmatics client and stop the receive loop."""
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception as exc:  # noqa: BLE001
                logger.debug("SpeechmaticsSTT disconnect error: %s", exc)
            self._client = None

        # Unblock any outstanding receive_transcripts() call.
        try:
            self._queue.put_nowait(self._STOP)
        except Exception:  # noqa: BLE001
            pass
