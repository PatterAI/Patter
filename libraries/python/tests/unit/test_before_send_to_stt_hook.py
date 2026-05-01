"""Unit tests for the ``before_send_to_stt`` pipeline hook (BUG #15).

The hook runs inside :meth:`PipelineStreamHandler.on_audio_received`
*after* mulaw decoding and resampling but *before* any bytes reach the
STT provider. It has three observable contracts:

  1. Returning modified bytes forwards the transformed buffer to STT —
     enabling custom noise cancellation / pre-emphasis / gain control.
  2. Returning ``None`` drops the chunk entirely — the STT provider
     must see zero bytes from that frame. This is the primary BUG #15
     regression: pre-fix, returning ``None`` was treated as an empty
     bytes payload which still hit the STT adapter.
  3. A hook that raises must fail *open* — the original (decoded) audio
     still passes through so a buggy user hook cannot silently kill the
     pipeline mid-call.

These tests patch the STT stub rather than mock the hook runner, so the
full call graph (hook executor → hook → on_audio_received → STT.send)
is exercised.
"""

from __future__ import annotations

from collections import deque
from unittest.mock import AsyncMock, MagicMock

import pytest

from getpatter.handlers.stream_handler import PipelineStreamHandler
from getpatter.models import PipelineHooks

from tests.conftest import fake_pcm_frame, make_agent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_handler(hooks: PipelineHooks | None) -> PipelineStreamHandler:
    audio_sender = AsyncMock()
    handler = PipelineStreamHandler(
        agent=make_agent(hooks=hooks),
        audio_sender=audio_sender,
        call_id="call-hook",
        caller="+15551110000",
        callee="+15552220000",
        resolved_prompt="p",
        metrics=None,
        for_twilio=False,  # so input_is_mulaw_8k defaults to False -> no transcode
        input_is_mulaw_8k=False,
        conversation_history=deque(maxlen=10),
        transcript_entries=deque(maxlen=10),
    )
    handler._stt = MagicMock()
    handler._stt.send_audio = AsyncMock()
    return handler


# ---------------------------------------------------------------------------
# Hook returning None drops the chunk
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestHookDropsChunk:
    """Hook returning ``None`` must prevent any bytes from reaching STT."""

    async def test_none_return_skips_stt_send(self) -> None:
        hooks = PipelineHooks(before_send_to_stt=lambda audio, ctx: None)
        handler = _make_handler(hooks)

        await handler.on_audio_received(fake_pcm_frame(duration_ms=20))

        handler._stt.send_audio.assert_not_awaited()

    async def test_none_return_from_async_hook_skips_stt(self) -> None:
        async def _async_hook(audio: bytes, ctx) -> None:
            return None

        hooks = PipelineHooks(before_send_to_stt=_async_hook)
        handler = _make_handler(hooks)

        await handler.on_audio_received(fake_pcm_frame(duration_ms=20))

        handler._stt.send_audio.assert_not_awaited()

    async def test_multiple_drops_never_reach_stt(self) -> None:
        """10 frames all dropped — STT must still see zero sends."""
        call_count = 0

        def _hook(audio: bytes, ctx) -> None:
            nonlocal call_count
            call_count += 1
            return None

        hooks = PipelineHooks(before_send_to_stt=_hook)
        handler = _make_handler(hooks)

        frame = fake_pcm_frame(duration_ms=20)
        for _ in range(10):
            await handler.on_audio_received(frame)

        assert call_count == 10  # hook was invoked every time
        handler._stt.send_audio.assert_not_awaited()


# ---------------------------------------------------------------------------
# Hook returning modified bytes forwards the new buffer
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestHookTransformsChunk:
    """Returning modified bytes must forward *those* bytes to STT."""

    async def test_sync_hook_bytes_forwarded(self) -> None:
        replacement = b"\xde\xad" * 80  # 160 bytes = 5 ms of PCM16 @ 16 kHz

        def _hook(audio: bytes, ctx) -> bytes:
            return replacement

        hooks = PipelineHooks(before_send_to_stt=_hook)
        handler = _make_handler(hooks)

        await handler.on_audio_received(fake_pcm_frame(duration_ms=20))

        handler._stt.send_audio.assert_awaited_once_with(replacement)

    async def test_async_hook_bytes_forwarded(self) -> None:
        replacement = b"\xbe\xef" * 40

        async def _hook(audio: bytes, ctx) -> bytes:
            return replacement

        hooks = PipelineHooks(before_send_to_stt=_hook)
        handler = _make_handler(hooks)

        await handler.on_audio_received(fake_pcm_frame(duration_ms=20))

        handler._stt.send_audio.assert_awaited_once_with(replacement)

    async def test_hook_receives_decoded_pcm(self) -> None:
        """The audio arg must be the PCM16 buffer (post-transcode), not mulaw."""
        seen: list[bytes] = []

        def _hook(audio: bytes, ctx) -> bytes:
            seen.append(audio)
            return audio

        hooks = PipelineHooks(before_send_to_stt=_hook)
        handler = _make_handler(hooks)
        frame = fake_pcm_frame(duration_ms=20)

        await handler.on_audio_received(frame)

        assert len(seen) == 1
        # frame is already PCM16 because input_is_mulaw_8k=False.
        assert seen[0] == frame


# ---------------------------------------------------------------------------
# Hook raising must fail-open (preserve original audio)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestHookFailOpen:
    """A user hook that raises must not break the pipeline."""

    async def test_raising_hook_passes_original_audio(self) -> None:
        def _bad_hook(audio: bytes, ctx) -> bytes:
            raise RuntimeError("user code is buggy")

        hooks = PipelineHooks(before_send_to_stt=_bad_hook)
        handler = _make_handler(hooks)
        frame = fake_pcm_frame(duration_ms=20)

        # Must not raise.
        await handler.on_audio_received(frame)

        handler._stt.send_audio.assert_awaited_once_with(frame)


# ---------------------------------------------------------------------------
# No hook configured → audio passes through unchanged
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestNoHook:
    """Agents without ``hooks`` must bypass the executor entirely."""

    async def test_no_hooks_sends_original_audio(self) -> None:
        handler = _make_handler(hooks=None)
        frame = fake_pcm_frame(duration_ms=20)

        await handler.on_audio_received(frame)

        handler._stt.send_audio.assert_awaited_once_with(frame)

    async def test_hooks_instance_without_before_send_to_stt(self) -> None:
        """Hooks configured but before_send_to_stt=None — still forwards."""
        hooks = PipelineHooks(before_send_to_stt=None)
        handler = _make_handler(hooks)
        frame = fake_pcm_frame(duration_ms=20)

        await handler.on_audio_received(frame)

        handler._stt.send_audio.assert_awaited_once_with(frame)
