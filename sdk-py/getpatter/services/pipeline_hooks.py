"""
Pipeline hook executor for pipeline mode.

Runs user-defined hooks at each stage of the STT → LLM → TTS pipeline.
Fail-open: if a hook throws, the error is logged and the original value
passes through unchanged.
"""

from __future__ import annotations

import inspect
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from getpatter.models import HookContext, PipelineHooks

logger = logging.getLogger("patter")


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

    def __init__(self, hooks: PipelineHooks | None) -> None:
        self._hooks = hooks

    async def run_before_send_to_stt(
        self, audio: bytes, ctx: HookContext
    ) -> bytes | None:
        """Run beforeSendToStt hook. Returns None to drop the audio chunk.

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
        self, transcript: str, ctx: HookContext
    ) -> str | None:
        """Run afterTranscribe hook. Returns None if hook vetoes the turn."""
        hook = self._hooks.after_transcribe if self._hooks else None
        if hook is None:
            return transcript
        try:
            return await _call_hook(hook, transcript, ctx)
        except Exception:
            logger.exception("Pipeline hook after_transcribe threw")
            return transcript

    async def run_before_synthesize(
        self, text: str, ctx: HookContext
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
        self, audio: bytes, text: str, ctx: HookContext
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
