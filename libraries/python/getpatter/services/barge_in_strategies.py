"""Barge-in confirmation strategies.

When a caller starts speaking while the agent's TTS is in flight, the SDK has
to decide whether the speech is a real interruption or just a brief
backchannel ("uh-huh", "okay") / room noise / cough. The default behaviour
is to treat any VAD speech_start as a confirmed barge-in and cancel the
agent immediately. That is fine for clean inputs but produces frequent
false positives on PSTN: the agent gets cut mid-sentence by background
chatter, breath, or filler words and never recovers the conversational
thread.

Each :class:`BargeInStrategy` is consulted on every STT transcript while a
barge-in is *pending* (VAD fired, but the agent has not yet been cancelled).
The first strategy that returns ``True`` confirms the barge-in; if none do
within the configured timeout the pending state is dropped and the agent
resumes streaming TTS as if nothing happened. With an empty
``barge_in_strategies`` tuple the SDK falls back to the legacy
"interrupt immediately on VAD" path, so adding strategies is a strict
opt-in.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

logger = logging.getLogger("getpatter")


@runtime_checkable
class BargeInStrategy(Protocol):
    """Decides whether a pending barge-in should be confirmed.

    Implementations are async-friendly and stateless across calls — every
    call gets its own copies of the strategies (deep-copied at call setup)
    so per-call accumulators are safe.

    Subclasses MUST implement ``evaluate``. ``reset`` is optional — the
    default no-op suits stateless strategies.
    """

    async def evaluate(
        self,
        *,
        transcript: str,
        is_interim: bool,
        agent_speaking: bool,
    ) -> bool:
        """Return ``True`` when this strategy considers the user's speech a
        confirmed barge-in.

        Args:
            transcript: The latest STT output text (interim or final).
            is_interim: ``True`` for interim partials, ``False`` for final
                transcripts. Strategies may choose to ignore one bucket.
            agent_speaking: Whether the agent's TTS is currently in flight.
                Strategies typically apply a stricter rule while the agent
                is talking and a permissive rule otherwise.

        Returns:
            ``True`` to confirm the barge-in (cancels agent TTS + flushes
            inbound buffer + dispatches the user transcript). ``False`` to
            keep waiting — the strategy will be consulted again on the next
            transcript event.
        """
        ...

    async def reset(self) -> None:
        """Drop any per-turn accumulator state.

        Called when the agent finishes speaking naturally (no barge-in)
        and when a pending barge-in times out without confirmation.
        Default implementation is a no-op.
        """
        ...


class MinWordsStrategy:
    """Confirm barge-in only after the caller has spoken ``min_words`` words.

    This filters short backchannels, single-word utterances, and stray
    transcription fragments that VAD picked up but were not real
    interruptions. While the agent is silent the strategy permits any
    speech to count (one word is enough), so the first user turn is
    not delayed.

    Args:
        min_words: Minimum word count required while the agent is
            speaking. Reasonable values are 2-5; 3 is a good starting
            point for production phone agents. Must be ``>= 1``.
        use_interim: When ``True`` (default), interim STT partials are
            evaluated as soon as they arrive. Set to ``False`` to wait
            for finals only — slower but free of partial-word noise on
            jittery STT providers.
    """

    def __init__(self, *, min_words: int, use_interim: bool = True) -> None:
        if min_words < 1:
            raise ValueError(f"min_words must be >= 1 (got {min_words})")
        self._min_words = min_words
        self._use_interim = use_interim

    async def evaluate(
        self,
        *,
        transcript: str,
        is_interim: bool,
        agent_speaking: bool,
    ) -> bool:
        if is_interim and not self._use_interim:
            return False
        threshold = self._min_words if agent_speaking else 1
        word_count = len(transcript.split())
        return word_count >= threshold

    async def reset(self) -> None:
        return None


async def evaluate_strategies(
    strategies: tuple[BargeInStrategy, ...],
    *,
    transcript: str,
    is_interim: bool,
    agent_speaking: bool,
) -> bool:
    """Short-circuit-OR composition: first strategy that confirms wins.

    Returns ``False`` for an empty tuple so callers can use the empty
    default to mean "no opt-in confirmation, fall back to legacy
    interrupt-on-VAD".
    """
    if not strategies:
        return False
    text = transcript or ""
    for strategy in strategies:
        try:
            if await strategy.evaluate(
                transcript=text,
                is_interim=is_interim,
                agent_speaking=agent_speaking,
            ):
                return True
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "BargeInStrategy %s raised; treating as 'do not confirm': %s",
                type(strategy).__name__,
                exc,
            )
    return False


async def reset_strategies(strategies: tuple[BargeInStrategy, ...]) -> None:
    """Call ``reset()`` on every strategy, swallowing per-strategy errors."""
    for strategy in strategies:
        try:
            await strategy.reset()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(
                "BargeInStrategy %s.reset() raised: %s",
                type(strategy).__name__,
                exc,
            )
