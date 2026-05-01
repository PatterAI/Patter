"""Lightweight synchronous/async event bus for Patter pipeline events."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Literal

logger = logging.getLogger(__name__)

PatterEventType = Literal[
    "turn_started",
    "turn_ended",
    "eou_metrics",
    "interruption",
    "llm_metrics",
    "tts_metrics",
    "stt_metrics",
    "metrics_collected",
    "call_ended",
    # Fine-grained pipeline events (additive — existing callbacks remain).
    "transcript_partial",
    "transcript_final",
    "llm_chunk",
    "tts_chunk",
    "tool_call_started",
]


class EventBus:
    """Lightweight event emitter.

    Emits are fire-and-forget; handlers that raise are caught and logged so
    a misbehaving observer never disrupts the call pipeline.

    Usage::

        bus = EventBus()
        unsubscribe = bus.on("turn_ended", lambda payload: print(payload))
        bus.emit("turn_ended", {"turn_index": 0})
        unsubscribe()  # remove listener
    """

    def __init__(self) -> None:
        self._listeners: dict[str, list[Callable[[Any], Any]]] = {}

    def on(
        self,
        event: PatterEventType,
        cb: Callable[[Any], Any],
    ) -> Callable[[], None]:
        """Register *cb* for *event*.

        Returns a zero-argument callable that removes the listener when called.
        """
        self._listeners.setdefault(event, []).append(cb)
        return lambda: self._listeners[event].remove(cb)

    def emit(self, event: PatterEventType, payload: Any) -> None:
        """Fire *event* with *payload* to all registered listeners.

        Synchronous callbacks are called inline. Async callbacks are scheduled
        via :func:`asyncio.create_task` (requires a running event loop).
        Exceptions are caught and logged; they never propagate to the caller.
        """
        for cb in list(self._listeners.get(event, [])):
            try:
                result = cb(payload)
                if hasattr(result, "__await__"):
                    asyncio.create_task(result)  # noqa: RUF006
            except Exception:
                logger.exception("event listener %s failed", event)
