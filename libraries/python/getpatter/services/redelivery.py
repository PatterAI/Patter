"""Re-delivery policy for opt-in barge-in re-delivery.

When a caller interrupts (barges in on) the agent mid-answer, the un-heard
remainder of the reply is normally dropped: the turn is cancelled and the
agent answers the caller's new message from scratch. Sometimes the un-heard
remainder still mattered (a price, a caveat, the second half of an answer).

With ``Agent.redeliver_interrupted`` on, the SDK captures the
``(heard, unsaid)`` split at the moment of a confirmed barge-in and, on the
NEXT user turn, injects a one-shot system nudge that asks the LLM to answer
the caller's new message first and then resume the unfinished idea naturally
(never mechanically repeat it, drop it if the caller has moved on).

A :class:`RedeliveryPolicy` decides whether a given interruption is *worth*
resuming. The default :class:`MinUnsaidWordsPolicy` arms re-delivery only
when the caller had NOT heard everything and the un-heard tail is at least
:data:`DEFAULT_MIN_UNSAID_WORDS` words — below that the remainder is a
trailing word or two and resuming it adds more noise than value. Users can
supply their own policy via ``Agent.redelivery_policy`` to change what
"worth resuming" means. Mirrors TS ``services/redelivery.ts``.

This is a pipeline-mode feature: the heard/unsaid split is derived from the
pipeline's per-sentence playback timeline. Realtime / ConvAI modes have no
such split and never arm re-delivery.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

logger = logging.getLogger("getpatter")

#: Minimum number of un-heard words for a re-delivery to be worth arming.
#: Below this the unfinished tail is trivial (a trailing word or two) and
#: resuming it would add more noise than value. A module constant so the
#: threshold is documented and overridable via a custom
#: :class:`RedeliveryPolicy`.
DEFAULT_MIN_UNSAID_WORDS = 3


@runtime_checkable
class RedeliveryPolicy(Protocol):
    """Decides whether an interrupted turn's un-heard remainder is worth
    resuming on the next turn.

    Implementations are pure and stateless — the decision is a function of
    the current split only. Mirror of the TS ``RedeliveryPolicy`` interface.
    """

    def should_redeliver(
        self, *, heard: str, unsaid: str, heard_everything: bool
    ) -> bool:
        """Return ``True`` to arm a re-delivery nudge for the next turn.

        Args:
            heard: The prefix of the reply the caller actually heard before
                interrupting (may be empty if they cut in immediately).
            unsaid: The un-heard remainder of the reply (word-boundary split).
            heard_everything: ``True`` when the caller heard the whole reply —
                there is nothing to resume, so policies should return
                ``False``.

        Returns:
            ``True`` to capture ``(heard, unsaid)`` and inject a one-shot
            re-delivery nudge on the next user turn. ``False`` to drop the
            un-heard remainder as today.
        """
        ...


class MinUnsaidWordsPolicy:
    """Arm re-delivery only when a non-trivial remainder went un-heard.

    Returns ``False`` when the caller heard everything (nothing to resume)
    and when the un-heard tail is shorter than ``min_words`` words. This is
    the default policy; the ``min_words`` gate keeps a stray trailing word
    or two from triggering a re-delivery.

    Args:
        min_words: Minimum un-heard word count required to arm re-delivery.
            Must be ``>= 1``. Defaults to :data:`DEFAULT_MIN_UNSAID_WORDS`.
    """

    def __init__(self, *, min_words: int = DEFAULT_MIN_UNSAID_WORDS) -> None:
        if min_words < 1:
            raise ValueError(f"min_words must be >= 1 (got {min_words})")
        self._min_words = min_words

    def should_redeliver(
        self, *, heard: str, unsaid: str, heard_everything: bool
    ) -> bool:
        if heard_everything:
            return False
        return len(unsaid.split()) >= self._min_words


#: Process-wide default policy instance (stateless, safe to share).
DEFAULT_REDELIVERY_POLICY: RedeliveryPolicy = MinUnsaidWordsPolicy()


def build_redelivery_nudge(heard: str, unsaid: str) -> str:
    """Build the one-shot system nudge injected on the turn after a barge-in.

    ``heard`` / ``unsaid`` are the caller-heard prefix and un-heard remainder
    captured at the interruption. When ``heard`` is empty the nudge phrases it
    as nothing-yet-heard. Mirrors TS ``buildRedeliveryNudge``.
    """
    heard_clause = (
        f"Recently heard by the user: {heard.strip()}."
        if heard.strip()
        else "The user had not yet heard any of it."
    )
    return (
        "You were interrupted while speaking. "
        f"{heard_clause} "
        f"Not yet heard by the user: {unsaid.strip()}. "
        "Answer the user's latest message first. Then, only if it is still "
        "relevant, resume the unfinished idea naturally — weave it in, do not "
        "mechanically repeat the unheard text, and drop it entirely if the "
        "caller has moved on."
    )
