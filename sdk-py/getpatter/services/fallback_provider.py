"""Fallback LLM provider that tries multiple providers in sequence.

If the primary provider fails, the next provider is tried, and so on.
Each provider gets a configurable number of retries before being skipped.
Failed providers are marked unavailable and periodically re-checked in the
background.
"""

from __future__ import annotations

__all__ = ["FallbackLLMProvider", "AllProvidersFailedError", "PartialStreamError"]

import asyncio
import logging
from typing import AsyncIterator

from getpatter.services.llm_loop import LLMProvider

logger = logging.getLogger("patter")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AllProvidersFailedError(Exception):
    """Raised when every provider in the fallback chain has failed."""


class PartialStreamError(Exception):
    """Raised when a provider fails after already yielding partial output."""


# ---------------------------------------------------------------------------
# FallbackLLMProvider
# ---------------------------------------------------------------------------


class FallbackLLMProvider:
    """LLM provider that tries multiple providers in sequence with failover.

    Args:
        providers: Ordered list of :class:`LLMProvider` instances.
        max_retry_per_provider: Number of attempts per provider before moving
            to the next (default ``1``).
        recovery_interval_s: Seconds between background recovery probes
            (default ``30``).
    """

    def __init__(
        self,
        providers: list[LLMProvider],
        *,
        max_retry_per_provider: int = 1,
        recovery_interval_s: float = 30.0,
    ) -> None:
        if not providers:
            raise ValueError("FallbackLLMProvider requires at least one provider")

        self._providers: list[LLMProvider] = list(providers)
        self._availability: list[bool] = [True] * len(providers)
        self._max_retry_per_provider = max_retry_per_provider
        self._recovery_interval_s = recovery_interval_s
        self._recovery_tasks: list[asyncio.Task[None] | None] = [None] * len(providers)

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def get_availability(self) -> list[bool]:
        """Return a snapshot of per-provider availability."""
        return list(self._availability)

    def destroy(self) -> None:
        """Cancel all background recovery tasks. Call on shutdown.

        Prefer :meth:`aclose` in async contexts — it awaits task cancellation
        and guarantees no pending tasks survive the owning event loop.
        """
        for i, task in enumerate(self._recovery_tasks):
            if task is not None:
                task.cancel()
                self._recovery_tasks[i] = None

    async def aclose(self) -> None:
        """Cancel probe tasks and await them. Safe to call multiple times."""
        tasks = [t for t in self._recovery_tasks if t is not None]
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._recovery_tasks = [None] * len(self._providers)

    async def __aenter__(self) -> "FallbackLLMProvider":
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # LLMProvider implementation
    # ------------------------------------------------------------------

    async def complete_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> AsyncIterator[str]:
        """Stream only the text deltas, flattening the chunk envelope.

        Convenience wrapper over :meth:`stream` for callers that only want the
        assistant's text output and don't need tool-call or done markers.
        Mirrors the TypeScript SDK's ``fallback.completeStream`` shape.
        """
        async for chunk in self.stream(messages, tools):
            if chunk.get("type") == "text":
                yield chunk.get("content", "")

    async def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> AsyncIterator[dict]:
        """Try providers in sequence, yielding chunks from the first that succeeds."""
        errors: list[Exception] = []

        # First pass: try available providers
        async for chunk in self._try_providers(
            messages, tools, available_only=True, errors=errors
        ):
            if isinstance(chunk, _Done):
                return
            yield chunk

        # All-failed fallback: retry every provider once more
        logger.warning(
            "FallbackLLMProvider: all providers unavailable, retrying all once"
        )
        async for chunk in self._try_providers(
            messages, tools, available_only=False, errors=errors
        ):
            if isinstance(chunk, _Done):
                return
            yield chunk

        raise AllProvidersFailedError(
            f"All {len(self._providers)} LLM providers failed. "
            f"Last error: {errors[-1] if errors else 'unknown'}"
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _try_providers(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        *,
        available_only: bool,
        errors: list[Exception],
    ) -> AsyncIterator[dict | _Done]:
        """Try each provider, yielding chunks or a _Done sentinel."""
        for i, provider in enumerate(self._providers):
            if available_only and not self._availability[i]:
                continue

            for attempt in range(self._max_retry_per_provider):
                try:
                    retry_label = f" (retry {attempt})" if attempt > 0 else ""
                    logger.info(
                        "FallbackLLMProvider: trying provider %d%s",
                        i,
                        retry_label,
                    )

                    yielded_tokens = False
                    async for chunk in provider.stream(messages, tools):
                        yield chunk
                        yielded_tokens = True

                    # Success — restore availability if needed
                    if not self._availability[i]:
                        self._availability[i] = True
                        self._stop_recovery(i)
                        logger.info(
                            "FallbackLLMProvider: provider %d recovered", i
                        )

                    yield _Done()
                    return

                except PartialStreamError:
                    raise

                except Exception as exc:
                    if yielded_tokens:
                        msg = (
                            f"FallbackLLMProvider: provider {i} failed after "
                            "yielding tokens — cannot retry"
                        )
                        logger.warning(msg)
                        raise PartialStreamError(msg) from exc

                    errors.append(exc)
                    logger.warning(
                        "FallbackLLMProvider: provider %d attempt %d failed — %s",
                        i,
                        attempt + 1,
                        exc,
                    )

            # Exhausted retries for this provider
            self._mark_unavailable(i)

    def _mark_unavailable(self, index: int) -> None:
        if not self._availability[index]:
            return
        self._availability[index] = False
        logger.warning(
            "FallbackLLMProvider: marking provider %d as unavailable", index
        )
        self._start_recovery(index)

    def _start_recovery(self, index: int) -> None:
        if self._recovery_tasks[index] is not None:
            return

        async def _probe() -> None:
            while True:
                await asyncio.sleep(self._recovery_interval_s)
                try:
                    logger.debug(
                        "FallbackLLMProvider: probing provider %d for recovery",
                        index,
                    )
                    gen = self._providers[index].stream(
                        [{"role": "user", "content": "ping"}], None
                    )
                    # Drain one chunk to verify the provider responds.
                    # Explicit aclose() ensures the underlying HTTP stream
                    # (e.g. OpenAI SDK) is released even when the async
                    # generator early-exits before normal completion.
                    try:
                        async for _ in gen:
                            break
                    finally:
                        await gen.aclose()

                    self._availability[index] = True
                    self._stop_recovery(index)
                    logger.info(
                        "FallbackLLMProvider: provider %d recovered", index
                    )
                    return
                except Exception:
                    pass  # Still unavailable — keep probing

        self._recovery_tasks[index] = asyncio.create_task(_probe())

    def _stop_recovery(self, index: int) -> None:
        task = self._recovery_tasks[index]
        if task is not None:
            task.cancel()
            self._recovery_tasks[index] = None


# ---------------------------------------------------------------------------
# Internal sentinel
# ---------------------------------------------------------------------------


class _Done:
    """Internal sentinel to signal successful completion."""
