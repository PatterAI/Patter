"""
IVR auto-navigation activity for telephony calls.

Detects IVR prompts via transcribed speech, forwards DTMF responses
through ``CallControl.send_dtmf``, and recovers from two common
failure modes:

1. The agent hears the same IVR prompt repeated several times (loop
   detection). ``TfidfLoopDetector`` flags this by comparing the
   cosine similarity of recent transcript chunks.
2. The IVR falls silent while both parties are passive (silence
   detection). A debounced timer triggers a follow-up action after
   ``max_silence_duration`` seconds of combined silence.

The public surface mirrors LiveKit ``IVRActivity`` but is wired to
Patter's ``CallControl`` abstraction rather than LiveKit rooms, so
transcripts are pushed in explicitly by the caller (typically
``PipelineStreamHandler``) through ``on_user_transcribed``.

Optional extras install::

    pip install "getpatter[ivr]"

Algorithm adapted from LiveKit Agents (Apache 2.0):
https://github.com/livekit/agents

Source:
- livekit-agents/livekit/agents/voice/ivr/ivr_activity.py
- livekit-agents/livekit/agents/beta/tools/send_dtmf.py
LiveKit SHA at port time: 78a66bcf79c5cea82989401c408f1dff4b961a5b
"""

from __future__ import annotations

import asyncio
import logging
from enum import Enum
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from getpatter.models import CallControl

logger = logging.getLogger("patter.ivr")


# ── DTMF event taxonomy ─────────────────────────────────────────────────

class DtmfEvent(str, Enum):
    """Valid DTMF tones. ``value`` is the literal keypad character.

    Kept as a string enum so the value doubles as the digit string
    forwarded to ``CallControl.send_dtmf``.
    """

    ONE = "1"
    TWO = "2"
    THREE = "3"
    FOUR = "4"
    FIVE = "5"
    SIX = "6"
    SEVEN = "7"
    EIGHT = "8"
    NINE = "9"
    ZERO = "0"
    STAR = "*"
    POUND = "#"
    A = "A"
    B = "B"
    C = "C"
    D = "D"


def format_dtmf(events: list[DtmfEvent]) -> str:
    """Join DTMF events into a space-separated debug string."""
    return " ".join(event.value for event in events)


# ── Debounced silence detector ──────────────────────────────────────────

class _DebouncedCall:
    """Schedule an async callback after a quiet period.

    Reset each time ``schedule()`` is called; the most recent schedule
    "wins". ``cancel()`` stops any pending call.

    This replaces LiveKit's ``Debounced`` helper without the LiveKit
    async primitives.
    """

    def __init__(
        self,
        callback: Callable[[], Awaitable[None]],
        delay: float,
    ) -> None:
        self._callback = callback
        self._delay = delay
        self._task: asyncio.Task[None] | None = None

    def schedule(self) -> None:
        self.cancel()
        self._task = asyncio.create_task(self._run())

    def cancel(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _run(self) -> None:
        try:
            await asyncio.sleep(self._delay)
        except asyncio.CancelledError:
            return

        try:
            await self._callback()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("IVR silence callback raised")


# ── TF-IDF loop detector ────────────────────────────────────────────────

class TfidfLoopDetector:
    """Detect repeated IVR prompts via TF-IDF cosine similarity.

    Args:
        window_size: Number of recent chunks to retain for comparison.
        similarity_threshold: Cosine similarity above which two chunks
            are considered "the same prompt". Range ``[0.0, 1.0]``.
        consecutive_threshold: How many consecutive near-duplicates
            must appear before ``check_loop_detection`` returns ``True``.

    Requires the optional ``ivr`` extras (``scikit-learn`` + ``numpy``).
    """

    def __init__(
        self,
        window_size: int = 20,
        similarity_threshold: float = 0.85,
        consecutive_threshold: int = 3,
    ) -> None:
        if window_size <= 0:
            raise ValueError("window_size must be greater than 0")
        if similarity_threshold < 0.0 or similarity_threshold > 1.0:
            raise ValueError("similarity_threshold must be between 0.0 and 1.0")
        if consecutive_threshold <= 0:
            raise ValueError("consecutive_threshold must be greater than 0")

        # Lazy import — keep sklearn/numpy out of the hot path import.
        try:
            import numpy as np  # noqa: F401
            from sklearn.feature_extraction.text import TfidfVectorizer  # noqa: F401
            from sklearn.metrics.pairwise import cosine_similarity  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "TfidfLoopDetector requires the 'ivr' extras. "
                "Install with: pip install 'getpatter[ivr]'"
            ) from exc

        self._window_size = window_size
        self._similarity_threshold = similarity_threshold
        self._consecutive_threshold = consecutive_threshold
        self._chunks: list[str] = []
        self._consecutive_similar = 0

    def reset(self) -> None:
        """Clear buffered history and consecutive counter."""
        self._chunks = []
        self._consecutive_similar = 0

    def add_chunk(self, chunk: str) -> None:
        """Append a transcript chunk, trimming to the window size."""
        self._chunks.append(chunk)
        if len(self._chunks) > self._window_size:
            self._chunks = self._chunks[-self._window_size :]

    def check_loop_detection(self) -> bool:
        """Return ``True`` when the last chunk has repeated enough times."""
        if len(self._chunks) < 2:
            return False

        import numpy as np
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity

        vectorizer = TfidfVectorizer()
        try:
            doc_matrix = vectorizer.fit_transform(self._chunks)
        except ValueError:
            # Empty vocabulary — all chunks were stop-words/empty.
            return False

        doc_similarity = cosine_similarity(doc_matrix)
        last_row = doc_similarity[-1][:-1]

        if last_row.size > 0 and np.max(last_row) > self._similarity_threshold:
            self._consecutive_similar += 1
        else:
            self._consecutive_similar = 0

        return self._consecutive_similar >= self._consecutive_threshold


# ── IVRActivity ─────────────────────────────────────────────────────────

# Callback signature: async () -> None
SilenceCallback = Callable[[], Awaitable[None]]
LoopCallback = Callable[[], Awaitable[None]]


class IVRActivity:
    """Coordinate IVR navigation heuristics for a single call.

    The activity does not own a timer loop while idle; silence checks
    are debounced and re-armed each time ``note_user_silent`` /
    ``note_agent_idle`` is called. Call sites (typically
    ``PipelineStreamHandler``) push transcripts and state updates into
    the activity; the activity reacts asynchronously.

    Args:
        call_control: The active call's control interface — used to
            forward DTMF digits.
        max_silence_duration: Seconds of combined quiet before the
            silence callback fires. Defaults to ``5.0``.
        loop_detector: Whether to enable TF-IDF loop detection.
            Requires the ``ivr`` extras. Defaults to ``True``.
        on_loop_detected: Async callback fired when the loop detector
            trips (e.g. re-prompt the IVR).
        on_silence: Async callback fired after ``max_silence_duration``
            of combined silence (e.g. speak a filler).

    Usage::

        call_control = ...  # provided by the handler
        ivr = IVRActivity(call_control)
        await ivr.start()

        # In the handler's STT loop:
        async def on_transcript(text: str) -> None:
            await ivr.on_user_transcribed(text)

        # When done:
        await ivr.stop()
    """

    def __init__(
        self,
        call_control: CallControl,
        *,
        max_silence_duration: float = 5.0,
        loop_detector: bool = True,
        on_loop_detected: LoopCallback | None = None,
        on_silence: SilenceCallback | None = None,
    ) -> None:
        self._call_control = call_control
        self._max_silence_duration = max_silence_duration
        self._loop_detector: TfidfLoopDetector | None = None
        if loop_detector:
            self._loop_detector = TfidfLoopDetector()
        self._on_loop_detected = on_loop_detected
        self._on_silence = on_silence

        self._current_user_state: str | None = None
        self._current_agent_state: str | None = None
        self._debounced_silence = _DebouncedCall(
            self._on_silence_detected,
            max_silence_duration,
        )
        self._last_should_schedule: bool | None = None
        self._started = False

    # ── lifecycle ────────────────────────────────────────────────────

    async def start(self) -> None:
        """Mark the activity as active. Idempotent."""
        self._started = True

    async def stop(self) -> None:
        """Cancel any pending silence timer. Idempotent."""
        self._debounced_silence.cancel()
        self._started = False

    # Backwards-compat alias mirroring the LiveKit ``aclose`` name.
    aclose = stop

    # ── external event hooks ─────────────────────────────────────────

    async def on_user_transcribed(self, text: str) -> None:
        """Feed a final user transcript to the activity.

        Called by ``PipelineStreamHandler`` whenever the STT emits a
        final transcript. A non-final transcript must NOT be passed.
        """
        if not self._started:
            return
        if not text:
            return

        if self._loop_detector is not None:
            self._loop_detector.add_chunk(text)
            if self._loop_detector.check_loop_detection():
                logger.debug("IVRActivity: speech loop detected")
                self._loop_detector.reset()
                if self._on_loop_detected is not None:
                    try:
                        await self._on_loop_detected()
                    except Exception:  # noqa: BLE001
                        logger.exception("IVR on_loop_detected callback raised")

    def note_user_state(self, state: str) -> None:
        """Record the user's latest voice activity state.

        ``state`` is one of ``"listening"``, ``"speaking"``, ``"away"``
        in the LiveKit taxonomy; any other value is treated as "active".
        """
        self._current_user_state = state
        self._schedule_silence_check()

    def note_agent_state(self, state: str) -> None:
        """Record the agent's latest state (``"idle"``, ``"speaking"``, ...)."""
        self._current_agent_state = state
        self._schedule_silence_check()

    # ── internals ────────────────────────────────────────────────────

    def _schedule_silence_check(self) -> None:
        should_schedule = self._should_schedule_check()
        if should_schedule:
            # Only (re)schedule if we weren't already waiting.
            if self._last_should_schedule:
                return
            self._debounced_silence.schedule()
        else:
            self._debounced_silence.cancel()
        self._last_should_schedule = should_schedule

    def _should_schedule_check(self) -> bool:
        is_user_silent = self._current_user_state in {"listening", "away"}
        is_agent_silent = self._current_agent_state in {"idle", "listening"}
        return is_user_silent and is_agent_silent

    async def _on_silence_detected(self) -> None:
        logger.debug("IVRActivity: silence detected")
        if self._on_silence is not None:
            try:
                await self._on_silence()
            except Exception:  # noqa: BLE001
                logger.exception("IVR on_silence callback raised")

    # ── LLM tool surface ─────────────────────────────────────────────

    @property
    def tools(self) -> list[dict[str, Any]]:
        """Return LLM function-call tool specs bound to this call.

        The returned dicts follow the OpenAI function-calling schema
        and include a ``handler`` coroutine that drives
        ``CallControl.send_dtmf``.
        """
        return [self._build_send_dtmf_tool()]

    def _build_send_dtmf_tool(self) -> dict[str, Any]:
        allowed_values = [e.value for e in DtmfEvent]

        async def handler(events: list[str]) -> str:
            # Validate each event — reject anything outside the enum.
            validated: list[str] = []
            for raw in events:
                if raw not in allowed_values:
                    return f"Failed to send DTMF event: invalid digit '{raw}'"
                validated.append(raw)

            digits = "".join(validated)
            try:
                await self._call_control.send_dtmf(digits, delay_ms=300)
            except Exception as exc:  # noqa: BLE001
                return f"Failed to send DTMF events: {exc}"
            return f"Successfully sent DTMF events: {' '.join(validated)}"

        return {
            "name": "send_dtmf_events",
            "description": (
                "Send a list of DTMF events to the telephony provider. "
                "Call when the IVR is asking for keypad input."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "events": {
                        "type": "array",
                        "description": "Ordered list of DTMF digits to send.",
                        "items": {
                            "type": "string",
                            "enum": allowed_values,
                        },
                    }
                },
                "required": ["events"],
            },
            "handler": handler,
        }


__all__ = [
    "DtmfEvent",
    "IVRActivity",
    "TfidfLoopDetector",
    "format_dtmf",
]
