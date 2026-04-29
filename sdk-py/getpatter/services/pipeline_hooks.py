"""
Pipeline hook executor for pipeline mode.

Runs user-defined hooks at each stage of the STT → LLM → TTS pipeline.
Fail-open: if a hook throws, the error is logged and the original value
passes through unchanged.

The ``after_llm`` hook accepts two shapes for backward compatibility:

* **Legacy callable** ``(text, ctx) → str | None`` — runs once at end of LLM
  stream, blocks streaming TTS. Mapped internally to the ``on_response`` slot
  of the new 3-tier API. Emits a one-shot deprecation warning on first use.
* **New 3-tier object** with optional ``on_chunk`` (sync, ~0 ms),
  ``on_sentence`` (async, ~50–300 ms, between chunker and TTS), and
  ``on_response`` (async, ~500 ms – 2 s, blocks streaming TTS).

The 3-tier object can be a plain ``dict`` with the corresponding keys, an
instance with attribute access (e.g., a ``dataclass``), or anything that
satisfies the ``AfterLLMHook`` Protocol.
"""

from __future__ import annotations

import inspect
import logging
import warnings
from typing import Any, Awaitable, Callable, Protocol, TYPE_CHECKING, runtime_checkable

if TYPE_CHECKING:
    from getpatter.models import HookContext, PipelineHooks

logger = logging.getLogger("getpatter")

_legacy_after_llm_warned = False


class PatterDeprecationWarning(DeprecationWarning, UserWarning):
    """Library-specific deprecation warning that is shown by default.

    Standard ``DeprecationWarning`` is silenced for non-``__main__`` callers
    in Python by default; subclassing ``UserWarning`` makes our notices
    actually surface in user code.
    """


@runtime_checkable
class AfterLLMHook(Protocol):
    """3-tier post-LLM transform protocol. All methods optional."""

    def on_chunk(self, chunk: str) -> str: ...
    async def on_sentence(self, sentence: str, ctx: "HookContext") -> str | None: ...
    async def on_response(self, text: str, ctx: "HookContext") -> str | None: ...


def _resolve_attr(obj: Any, name: str) -> Callable | None:
    """Return ``obj.name`` (or ``obj[name]`` for dicts) if callable, else None."""
    if isinstance(obj, dict):
        candidate = obj.get(name)
    else:
        candidate = getattr(obj, name, None)
    return candidate if callable(candidate) else None


def _normalise_after_llm(hook: Any) -> dict[str, Callable | None]:
    """Normalise ``after_llm`` into a 3-tier slot dict.

    Accepts:
    * ``None`` — returns an all-``None`` dict.
    * Plain callable ``(text, ctx) → str`` — mapped to ``on_response`` with a
      one-shot deprecation warning.
    * ``dict`` with ``on_chunk`` / ``on_sentence`` / ``on_response`` keys.
    * Any object exposing those attribute names (e.g. dataclass, Protocol).
    """
    global _legacy_after_llm_warned

    slots: dict[str, Callable | None] = {
        "on_chunk": None,
        "on_sentence": None,
        "on_response": None,
    }
    if hook is None:
        return slots

    # Legacy single callable form.
    if callable(hook) and not isinstance(hook, dict) and not _has_tier_attrs(hook):
        if not _legacy_after_llm_warned:
            _legacy_after_llm_warned = True
            warnings.warn(
                "after_llm: (text, ctx) -> str is deprecated; pass an object with "
                "on_response (and optionally on_chunk / on_sentence) instead. The "
                "legacy form maps to on_response and blocks streaming TTS. "
                "Will be removed in v0.7.0.",
                PatterDeprecationWarning,
                stacklevel=3,
            )
        slots["on_response"] = hook
        return slots

    # 3-tier object / dict.
    slots["on_chunk"] = _resolve_attr(hook, "on_chunk")
    slots["on_sentence"] = _resolve_attr(hook, "on_sentence")
    slots["on_response"] = _resolve_attr(hook, "on_response")
    return slots


def _has_tier_attrs(hook: Any) -> bool:
    """True iff ``hook`` exposes at least one of on_chunk / on_sentence / on_response."""
    if isinstance(hook, dict):
        return any(k in hook for k in ("on_chunk", "on_sentence", "on_response"))
    return any(hasattr(hook, k) for k in ("on_chunk", "on_sentence", "on_response"))


async def _call_hook(hook, *args):
    """Call a hook that may be sync or async. Returns the result.

    Uses ``inspect.isawaitable`` on the return value instead of
    ``asyncio.iscoroutinefunction`` to correctly handle
    ``functools.partial``, class instances with ``__call__``, and
    decorated async functions.
    """
    result = hook(*args)
    if inspect.isawaitable(result):
        return await result
    return result


class PipelineHookExecutor:
    """Executes pipeline hooks with fail-open semantics.

    If no hooks are configured, all methods are pass-through (return
    the input value unchanged). If a hook raises an exception, the
    error is logged and the original value passes through.
    """

    def __init__(self, hooks: "PipelineHooks | None") -> None:
        self._hooks = hooks
        raw_after_llm = hooks.after_llm if hooks is not None else None
        self._after_llm = _normalise_after_llm(raw_after_llm)

    async def run_before_send_to_stt(
        self, audio: bytes, ctx: "HookContext"
    ) -> bytes | None:
        """Run before_send_to_stt hook. Returns None to drop the audio chunk.

        Fail-open: if the hook raises, the original audio passes through.
        """
        hook = self._hooks.before_send_to_stt if self._hooks else None
        if hook is None:
            return audio
        try:
            return await _call_hook(hook, audio, ctx)
        except Exception:
            logger.exception("Pipeline hook before_send_to_stt threw")
            return audio

    async def run_after_transcribe(
        self, transcript: str, ctx: "HookContext"
    ) -> str | None:
        """Run after_transcribe hook. Returns None if hook vetoes the turn."""
        hook = self._hooks.after_transcribe if self._hooks else None
        if hook is None:
            return transcript
        try:
            return await _call_hook(hook, transcript, ctx)
        except Exception:
            logger.exception("Pipeline hook after_transcribe threw")
            return transcript

    async def run_before_llm(
        self, messages: list[dict], ctx: "HookContext"
    ) -> list[dict]:
        """Run before_llm hook. Returns a possibly-modified messages list.

        ``None`` from the hook means "keep the original" (no veto semantic
        — LLM calls are too important to silently drop).
        Fail-open: if the hook raises, the original messages pass through.
        """
        hook = self._hooks.before_llm if self._hooks else None
        if hook is None:
            return messages
        try:
            result = await _call_hook(hook, messages, ctx)
        except Exception:
            logger.exception("Pipeline hook before_llm threw")
            return messages
        if result is None:
            return messages
        return result

    # ---- after_llm 3-tier API -------------------------------------------------

    def run_after_llm_chunk(self, chunk: str) -> str:
        """Tier 1 — per-token sync transform (~0 ms budget).

        Returns the (possibly transformed) chunk. Fail-open: on exception or
        non-string return the original chunk passes through unchanged.
        """
        hook = self._after_llm["on_chunk"]
        if hook is None:
            return chunk
        try:
            result = hook(chunk)
            return result if isinstance(result, str) else chunk
        except Exception:
            logger.exception("Pipeline hook after_llm.on_chunk threw")
            return chunk

    async def run_after_llm_sentence(
        self, sentence: str, ctx: "HookContext"
    ) -> str | None:
        """Tier 2 — per-sentence rewrite (~50–300 ms).

        Returns the rewritten sentence text, the original sentence (when the
        hook returns ``None``), or ``None`` to drop the sentence (empty
        string is treated as drop). Fail-open.
        """
        hook = self._after_llm["on_sentence"]
        if hook is None:
            return sentence
        try:
            result = await _call_hook(hook, sentence, ctx)
        except Exception:
            logger.exception("Pipeline hook after_llm.on_sentence threw")
            return sentence
        if result is None:
            return sentence  # null = keep original
        if result == "":
            return None  # empty = drop
        return result

    async def run_after_llm_response(
        self, text: str, ctx: "HookContext"
    ) -> str:
        """Tier 3 — per-response rewrite (~500 ms – 2 s).

        Triggered after the LLM stream completes. Caller is responsible for
        buffering tokens before invocation. Fail-open: on exception the
        original text passes through.
        """
        hook = self._after_llm["on_response"]
        if hook is None:
            return text
        try:
            result = await _call_hook(hook, text, ctx)
        except Exception:
            logger.exception("Pipeline hook after_llm.on_response threw")
            return text
        if result is None:
            return text
        return result

    async def run_after_llm(self, text: str, ctx: "HookContext") -> str:
        """Backward-compatible alias for :meth:`run_after_llm_response`.

        .. deprecated:: 0.6.0
            Use :meth:`run_after_llm_response`.
        """
        return await self.run_after_llm_response(text, ctx)

    def has_after_llm_response(self) -> bool:
        """Whether tier 3 (``on_response``) is configured.

        The LLM loop uses this to decide whether to buffer streaming tokens
        before yielding them. Tier 1 / 2 do NOT require buffering.
        """
        return self._after_llm["on_response"] is not None

    def has_after_llm_sentence(self) -> bool:
        """Whether tier 2 (``on_sentence``) is configured."""
        return self._after_llm["on_sentence"] is not None

    def has_after_llm_chunk(self) -> bool:
        """Whether tier 1 (``on_chunk``) is configured."""
        return self._after_llm["on_chunk"] is not None

    def has_after_llm(self) -> bool:
        """Backward-compatible alias for :meth:`has_after_llm_response`.

        .. deprecated:: 0.6.0
            Use :meth:`has_after_llm_response`.
        """
        return self.has_after_llm_response()

    async def run_before_synthesize(
        self, text: str, ctx: "HookContext"
    ) -> str | None:
        """Run beforeSynthesize hook. Returns None if hook vetoes TTS."""
        hook = self._hooks.before_synthesize if self._hooks else None
        if hook is None:
            return text
        try:
            return await _call_hook(hook, text, ctx)
        except Exception:
            logger.exception("Pipeline hook before_synthesize threw")
            return text

    async def run_after_synthesize(
        self, audio: bytes, text: str, ctx: "HookContext"
    ) -> bytes | None:
        """Run afterSynthesize hook. Returns None if hook vetoes audio chunk."""
        hook = self._hooks.after_synthesize if self._hooks else None
        if hook is None:
            return audio
        try:
            return await _call_hook(hook, audio, text, ctx)
        except Exception:
            logger.exception("Pipeline hook after_synthesize threw")
            return audio
