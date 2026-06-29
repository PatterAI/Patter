"""Token-aware conversation-history compaction for pipeline mode.

This module adds two capabilities around the existing per-call history
representation (the ``deque`` owned by :class:`StreamHandler`):

1. **Token counting** — :func:`estimate_tokens` / :func:`estimate_messages_tokens`
   estimate prompt size in the canonical OpenAI ``chars / 4`` unit (the same
   baseline the SDK already uses for fallback billing in ``llm_loop.py``). No
   heavyweight tokenizer dependency is pulled in.

2. **Hybrid compaction** — :class:`ContextCompactor` keeps the most recent
   ``keep_last_turns`` turns VERBATIM and folds the older turns into a rolling
   summary once the estimated size of the summarizable portion crosses a token
   budget. The summary is produced by an injected async *summarizer* (the
   agent's own LLM by default) and the summarized entries are pruned from the
   working ``deque`` so the next turn's prompt stays bounded.

The compactor is intentionally decoupled from the LLM loop: it receives a
``summarizer`` callable and operates on a generic ``deque`` of
``{role, text, timestamp}`` entries, so the partition / trigger / prune logic
is plain, testable code with the LLM as the only external boundary.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from typing import Any

from getpatter.models import CompactionConfig

logger = logging.getLogger("getpatter")

__all__ = [
    "estimate_tokens",
    "estimate_messages_tokens",
    "ContextCompactor",
]

# Per-message structural overhead (role tag + delimiters) in estimated tokens.
# Mirrors the small fixed cost real tokenizers add per chat message so the
# estimate doesn't under-count many short turns.
_PER_MESSAGE_OVERHEAD = 4

# A summarizer takes ``(prior_summary, old_messages)`` and returns the new
# rolling summary text.
Summarizer = Callable[[str, "list[dict[str, Any]]"], Awaitable[str]]


def estimate_tokens(text: str | None) -> int:
    """Estimate the token count of ``text`` using the ``chars / 4`` baseline.

    This is the canonical rough OpenAI-tokenizer estimate already used for the
    fallback-billing path in ``llm_loop.py``. Empty/``None`` text is 0 tokens.
    """
    if not text:
        return 0
    return max(1, len(text) // 4)


def estimate_messages_tokens(messages: Iterable[Mapping[str, Any]]) -> int:
    """Estimate the total token count of a list of chat/history messages.

    Accepts both provider-format messages (``{"content": ...}``) and internal
    history entries (``{"text": ...}``). Adds a small per-message overhead for
    role tags / delimiters.
    """
    total = 0
    for msg in messages:
        content = msg.get("content")
        if content is None:
            content = msg.get("text", "")
        if not isinstance(content, str):
            content = "" if content is None else str(content)
        total += estimate_tokens(content) + _PER_MESSAGE_OVERHEAD
    return total


def _format_old_messages(old_messages: Sequence[Mapping[str, Any]]) -> str:
    """Render the to-be-summarized entries as a plain transcript for the LLM."""
    lines: list[str] = []
    for msg in old_messages:
        role = str(msg.get("role", "user"))
        text = msg.get("text")
        if text is None:
            text = msg.get("content", "")
        lines.append(f"{role}: {text}")
    return "\n".join(lines)


class ContextCompactor:
    """Token-budget-driven rolling summarizer for a per-call history deque.

    Holds the rolling ``summary`` string and a re-entrancy guard so at most one
    summarization runs at a time. The summary is read by the LLM loop (via
    :meth:`StreamHandler` wiring) and prepended to every prompt; the
    summarized entries are removed from the working deque by :meth:`maybe_compact`.
    """

    def __init__(self, config: CompactionConfig, summarizer: Summarizer) -> None:
        self._config = config
        self._summarizer = summarizer
        self._summary: str = ""
        self._compacting: bool = False

    @property
    def summary(self) -> str:
        """The current rolling summary ("" until the first compaction)."""
        return self._summary

    @property
    def is_compacting(self) -> bool:
        """True while a background summarization is in flight."""
        return self._compacting

    def _keep_messages(self) -> int:
        """Number of trailing history entries kept verbatim (>= 2)."""
        # A "turn" is approximated as a user+assistant pair. Floor at 1 turn
        # (2 messages) so the model always sees at least the last exchange.
        return max(1, self._config.keep_last_turns) * 2

    def should_compact(self, history: Sequence[Mapping[str, Any]]) -> bool:
        """Whether the summarizable portion has crossed ``trigger_tokens``.

        The summarizable portion is everything EXCEPT the trailing
        ``keep_last_turns`` turns, plus the prior summary (which is re-folded).
        """
        keep = self._keep_messages()
        if len(history) <= keep:
            return False
        old = list(history)[: len(history) - keep]
        if not old:
            return False
        old_tokens = estimate_messages_tokens(old)
        total = old_tokens + estimate_tokens(self._summary)
        return total >= self._config.trigger_tokens

    async def maybe_compact(self, history: "deque[dict[str, Any]]") -> str | None:
        """Summarize and prune the oldest entries when over budget.

        Non-blocking by design: intended to be scheduled as a background task
        so the live turn never waits on the summarizer. Returns the new summary
        when a compaction ran, else ``None`` (short call / already running).

        The trailing ``keep_last_turns`` turns are kept verbatim; the older
        entries are folded into the rolling summary and removed from ``history``
        (FIFO ``popleft``). Entries appended to the deque while the summarizer
        awaits stay untouched.
        """
        if self._compacting:
            return None
        if not self.should_compact(history):
            return None

        self._compacting = True
        try:
            keep = self._keep_messages()
            num_old = max(0, len(history) - keep)
            if num_old <= 0:
                return None
            old = list(history)[:num_old]

            new_summary = await self._summarizer(self._summary, old)
            new_summary = (new_summary or "").strip()
            if not new_summary:
                # Summarizer produced nothing usable — do NOT prune, or the
                # turns would be lost with no summary to replace them.
                logger.warning(
                    "context_compaction_empty_summary kept=%d old=%d", keep, num_old
                )
                return None

            # Prune exactly the entries we summarized. Appends during the await
            # landed at the right end of the deque, so the leftmost ``num_old``
            # are still the summarized ones.
            for _ in range(min(num_old, len(history))):
                history.popleft()

            self._summary = new_summary
            logger.info(
                "context_compaction summarized=%d kept=%d summary_tokens=%d",
                num_old,
                len(history),
                estimate_tokens(new_summary),
            )
            return new_summary
        finally:
            self._compacting = False
