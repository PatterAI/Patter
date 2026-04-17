"""Unit tests for PipelineHookExecutor.

Covers:
  1.  No hooks defined (None) — all methods pass through input unchanged
  2.  PipelineHooks object with no specific hook — returns input unchanged
  3.  afterTranscribe modifies the transcript
  4.  afterTranscribe returns None — method returns None
  5.  beforeSynthesize modifies the text
  6.  beforeSynthesize returns None — method returns None
  7.  afterSynthesize modifies the audio buffer
  8.  afterSynthesize returns None — method returns None
  9.  Hook raises an exception — fail-open: error logged, original value returned
 10.  Async hook — coroutine result is awaited correctly
 11.  Sync hook — regular callable works without await
 12.  HookContext fields — context object carries the expected attributes
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from patter.models import HookContext, PipelineHooks
from patter.services.pipeline_hooks import PipelineHookExecutor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_ctx(**kwargs) -> HookContext:
    defaults = dict(
        call_id="call-001",
        caller="+15550001111",
        callee="+15552223333",
        history=({"role": "user", "text": "Hello"},),
    )
    defaults.update(kwargs)
    return HookContext(**defaults)


# ---------------------------------------------------------------------------
# 1. No hooks defined (hooks=None)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestNoHooksDefined:
    """When PipelineHookExecutor is created with hooks=None every method is a
    simple pass-through — it must return the input value unchanged."""

    async def test_run_after_transcribe_returns_transcript(self):
        executor = PipelineHookExecutor(None)
        ctx = make_ctx()
        result = await executor.run_after_transcribe("hello world", ctx)
        assert result == "hello world"

    async def test_run_before_synthesize_returns_text(self):
        executor = PipelineHookExecutor(None)
        ctx = make_ctx()
        result = await executor.run_before_synthesize("speak this", ctx)
        assert result == "speak this"

    async def test_run_after_synthesize_returns_audio(self):
        executor = PipelineHookExecutor(None)
        ctx = make_ctx()
        audio = b"\x00\x01\x02"
        result = await executor.run_after_synthesize(audio, "speak this", ctx)
        assert result == audio


# ---------------------------------------------------------------------------
# 2. PipelineHooks object present but the specific hook is None
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSpecificHookNotDefined:
    """When a PipelineHooks dataclass exists but the relevant hook attribute is
    None the executor must still return the input unchanged."""

    async def test_after_transcribe_absent(self):
        hooks = PipelineHooks(after_transcribe=None)
        executor = PipelineHookExecutor(hooks)
        result = await executor.run_after_transcribe("text", make_ctx())
        assert result == "text"

    async def test_before_synthesize_absent(self):
        hooks = PipelineHooks(before_synthesize=None)
        executor = PipelineHookExecutor(hooks)
        result = await executor.run_before_synthesize("sentence", make_ctx())
        assert result == "sentence"

    async def test_after_synthesize_absent(self):
        hooks = PipelineHooks(after_synthesize=None)
        executor = PipelineHookExecutor(hooks)
        audio = b"\xff\xfe"
        result = await executor.run_after_synthesize(audio, "text", make_ctx())
        assert result == audio


# ---------------------------------------------------------------------------
# 3. afterTranscribe modifies the transcript
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_after_transcribe_modifies_transcript():
    def upper_hook(transcript: str, ctx: HookContext) -> str:
        return transcript.upper()

    hooks = PipelineHooks(after_transcribe=upper_hook)
    executor = PipelineHookExecutor(hooks)
    result = await executor.run_after_transcribe("hello", make_ctx())
    assert result == "HELLO"


# ---------------------------------------------------------------------------
# 4. afterTranscribe returns None — hook vetoes the turn
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_after_transcribe_returns_none():
    def veto_hook(transcript: str, ctx: HookContext):
        return None

    hooks = PipelineHooks(after_transcribe=veto_hook)
    executor = PipelineHookExecutor(hooks)
    result = await executor.run_after_transcribe("hello", make_ctx())
    assert result is None


# ---------------------------------------------------------------------------
# 5. beforeSynthesize modifies the text
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_before_synthesize_modifies_text():
    def prefix_hook(text: str, ctx: HookContext) -> str:
        return f"[FILTERED] {text}"

    hooks = PipelineHooks(before_synthesize=prefix_hook)
    executor = PipelineHookExecutor(hooks)
    result = await executor.run_before_synthesize("say this", make_ctx())
    assert result == "[FILTERED] say this"


# ---------------------------------------------------------------------------
# 6. beforeSynthesize returns None — hook vetoes TTS
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_before_synthesize_returns_none():
    def veto_hook(text: str, ctx: HookContext):
        return None

    hooks = PipelineHooks(before_synthesize=veto_hook)
    executor = PipelineHookExecutor(hooks)
    result = await executor.run_before_synthesize("say this", make_ctx())
    assert result is None


# ---------------------------------------------------------------------------
# 7. afterSynthesize modifies the audio buffer
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_after_synthesize_modifies_audio():
    modified_audio = b"\xAA\xBB\xCC"

    def modify_hook(audio: bytes, text: str, ctx: HookContext) -> bytes:
        return modified_audio

    hooks = PipelineHooks(after_synthesize=modify_hook)
    executor = PipelineHookExecutor(hooks)
    result = await executor.run_after_synthesize(b"\x00\x01", "text", make_ctx())
    assert result == modified_audio


# ---------------------------------------------------------------------------
# 8. afterSynthesize returns None — hook discards the audio chunk
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_after_synthesize_returns_none():
    def discard_hook(audio: bytes, text: str, ctx: HookContext):
        return None

    hooks = PipelineHooks(after_synthesize=discard_hook)
    executor = PipelineHookExecutor(hooks)
    result = await executor.run_after_synthesize(b"\x00\x01", "text", make_ctx())
    assert result is None


# ---------------------------------------------------------------------------
# 9. Hook throws — fail-open: logs error, returns original value
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestHookThrows:
    """A crashing hook must not propagate the exception. The executor logs it
    and falls back to the original input value (fail-open semantics)."""

    async def test_after_transcribe_fail_open(self, caplog):
        def boom(transcript, ctx):
            raise RuntimeError("boom!")

        hooks = PipelineHooks(after_transcribe=boom)
        executor = PipelineHookExecutor(hooks)
        with caplog.at_level(logging.ERROR, logger="patter"):
            result = await executor.run_after_transcribe("original", make_ctx())

        assert result == "original"
        assert any("after_transcribe" in r.message for r in caplog.records)

    async def test_before_synthesize_fail_open(self, caplog):
        def boom(text, ctx):
            raise ValueError("bad value")

        hooks = PipelineHooks(before_synthesize=boom)
        executor = PipelineHookExecutor(hooks)
        with caplog.at_level(logging.ERROR, logger="patter"):
            result = await executor.run_before_synthesize("original", make_ctx())

        assert result == "original"
        assert any("before_synthesize" in r.message for r in caplog.records)

    async def test_after_synthesize_fail_open(self, caplog):
        original_audio = b"\x00\x01\x02"

        def boom(audio, text, ctx):
            raise Exception("crash")

        hooks = PipelineHooks(after_synthesize=boom)
        executor = PipelineHookExecutor(hooks)
        with caplog.at_level(logging.ERROR, logger="patter"):
            result = await executor.run_after_synthesize(original_audio, "text", make_ctx())

        assert result == original_audio
        assert any("after_synthesize" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 10. Async hook — coroutine result is awaited correctly
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestAsyncHook:
    """Hooks declared as async coroutines must be awaited and their return
    values propagated without wrapping."""

    async def test_async_after_transcribe(self):
        async def async_hook(transcript: str, ctx: HookContext) -> str:
            return transcript + " (async)"

        hooks = PipelineHooks(after_transcribe=async_hook)
        executor = PipelineHookExecutor(hooks)
        result = await executor.run_after_transcribe("hello", make_ctx())
        assert result == "hello (async)"

    async def test_async_before_synthesize(self):
        async def async_hook(text: str, ctx: HookContext) -> str:
            return text.strip()

        hooks = PipelineHooks(before_synthesize=async_hook)
        executor = PipelineHookExecutor(hooks)
        result = await executor.run_before_synthesize("  spaces  ", make_ctx())
        assert result == "spaces"

    async def test_async_after_synthesize(self):
        new_audio = b"\x11\x22"

        async def async_hook(audio: bytes, text: str, ctx: HookContext) -> bytes:
            return new_audio

        hooks = PipelineHooks(after_synthesize=async_hook)
        executor = PipelineHookExecutor(hooks)
        result = await executor.run_after_synthesize(b"\x00", "text", make_ctx())
        assert result == new_audio

    async def test_async_hook_returning_none(self):
        async def async_veto(transcript: str, ctx: HookContext):
            return None

        hooks = PipelineHooks(after_transcribe=async_veto)
        executor = PipelineHookExecutor(hooks)
        result = await executor.run_after_transcribe("anything", make_ctx())
        assert result is None

    async def test_async_hook_that_throws_is_fail_open(self, caplog):
        async def async_boom(transcript: str, ctx: HookContext) -> str:
            raise RuntimeError("async boom")

        hooks = PipelineHooks(after_transcribe=async_boom)
        executor = PipelineHookExecutor(hooks)
        with caplog.at_level(logging.ERROR, logger="patter"):
            result = await executor.run_after_transcribe("original", make_ctx())

        assert result == "original"


# ---------------------------------------------------------------------------
# 10b. functools.partial and class callables — regression tests for isawaitable fix
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestCallableEdgeCases:
    """Verify hooks work with functools.partial and class-based callables.
    These are the exact cases that broke with asyncio.iscoroutinefunction."""

    async def test_functools_partial_wrapping_async(self):
        import functools

        async def async_hook(prefix: str, transcript: str, ctx: HookContext) -> str:
            return f"{prefix}: {transcript}"

        partial_hook = functools.partial(async_hook, "PREFIX")
        hooks = PipelineHooks(after_transcribe=partial_hook)
        executor = PipelineHookExecutor(hooks)
        result = await executor.run_after_transcribe("hello", make_ctx())
        assert result == "PREFIX: hello"

    async def test_class_with_async_call(self):
        class MyHook:
            async def __call__(self, transcript: str, ctx: HookContext) -> str:
                return transcript.upper()

        hooks = PipelineHooks(after_transcribe=MyHook())
        executor = PipelineHookExecutor(hooks)
        result = await executor.run_after_transcribe("hello", make_ctx())
        assert result == "HELLO"

    async def test_functools_partial_wrapping_sync(self):
        import functools

        def sync_hook(suffix: str, transcript: str, ctx: HookContext) -> str:
            return f"{transcript}{suffix}"

        partial_hook = functools.partial(sync_hook, "!")
        hooks = PipelineHooks(after_transcribe=partial_hook)
        executor = PipelineHookExecutor(hooks)
        result = await executor.run_after_transcribe("hi", make_ctx())
        assert result == "hi!"


# ---------------------------------------------------------------------------
# 11. Sync hook — regular (non-async) callable works correctly
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSyncHook:
    """Plain synchronous callables (not coroutines) must be called without
    await and their return values propagated normally."""

    async def test_sync_after_transcribe(self):
        call_count = {"n": 0}

        def sync_hook(transcript: str, ctx: HookContext) -> str:
            call_count["n"] += 1
            return "synced"

        hooks = PipelineHooks(after_transcribe=sync_hook)
        executor = PipelineHookExecutor(hooks)
        result = await executor.run_after_transcribe("input", make_ctx())
        assert result == "synced"
        assert call_count["n"] == 1

    async def test_sync_before_synthesize(self):
        def sync_hook(text: str, ctx: HookContext) -> str:
            return text.lower()

        hooks = PipelineHooks(before_synthesize=sync_hook)
        executor = PipelineHookExecutor(hooks)
        result = await executor.run_before_synthesize("HELLO", make_ctx())
        assert result == "hello"

    async def test_sync_after_synthesize(self):
        def sync_hook(audio: bytes, text: str, ctx: HookContext) -> bytes:
            return bytes(reversed(audio))

        hooks = PipelineHooks(after_synthesize=sync_hook)
        executor = PipelineHookExecutor(hooks)
        audio = b"\x01\x02\x03"
        result = await executor.run_after_synthesize(audio, "text", make_ctx())
        assert result == bytes(reversed(audio))


# ---------------------------------------------------------------------------
# 12. HookContext fields — verify the context object has the expected shape
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestHookContextFields:
    """The HookContext passed to every hook must carry the call metadata that
    the executor receives. Here we verify the fields are accessible from inside
    the hook callback."""

    async def test_context_fields_visible_in_after_transcribe(self):
        received: list[HookContext] = []

        def capture_hook(transcript: str, ctx: HookContext) -> str:
            received.append(ctx)
            return transcript

        ctx = make_ctx(
            call_id="call-xyz",
            caller="+15550000001",
            callee="+15559999999",
            history=({"role": "user", "text": "hi"},),
        )
        hooks = PipelineHooks(after_transcribe=capture_hook)
        executor = PipelineHookExecutor(hooks)
        await executor.run_after_transcribe("test", ctx)

        assert len(received) == 1
        captured = received[0]
        assert captured.call_id == "call-xyz"
        assert captured.caller == "+15550000001"
        assert captured.callee == "+15559999999"
        assert captured.history == ({"role": "user", "text": "hi"},)

    async def test_context_fields_visible_in_before_synthesize(self):
        received: list[HookContext] = []

        def capture_hook(text: str, ctx: HookContext) -> str:
            received.append(ctx)
            return text

        ctx = make_ctx(call_id="call-abc")
        hooks = PipelineHooks(before_synthesize=capture_hook)
        executor = PipelineHookExecutor(hooks)
        await executor.run_before_synthesize("text", ctx)

        assert received[0].call_id == "call-abc"

    async def test_context_fields_visible_in_after_synthesize(self):
        received: list[HookContext] = []

        def capture_hook(audio: bytes, text: str, ctx: HookContext) -> bytes:
            received.append(ctx)
            return audio

        ctx = make_ctx(call_id="call-qrs", caller="+1111", callee="+2222")
        hooks = PipelineHooks(after_synthesize=capture_hook)
        executor = PipelineHookExecutor(hooks)
        await executor.run_after_synthesize(b"\x00", "text", ctx)

        assert received[0].call_id == "call-qrs"
        assert received[0].caller == "+1111"
        assert received[0].callee == "+2222"

    def test_hook_context_is_immutable(self):
        ctx = make_ctx()
        with pytest.raises(Exception):
            ctx.call_id = "mutated"  # type: ignore[misc]

    def test_hook_context_default_history(self):
        ctx = HookContext(call_id="c", caller="a", callee="b")
        assert ctx.history == ()


# ---------------------------------------------------------------------------
# 13. before_send_to_stt hook (audio chunk interceptor)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestBeforeSendToStt:
    """The new ``before_send_to_stt`` hook intercepts raw PCM audio before it
    is forwarded to the STT provider. Returning None drops the chunk."""

    async def test_no_hooks_returns_audio(self):
        executor = PipelineHookExecutor(None)
        ctx = make_ctx()
        audio = b"\x00\x01\x02"
        assert await executor.run_before_send_to_stt(audio, ctx) == audio

    async def test_hook_not_defined_returns_audio(self):
        executor = PipelineHookExecutor(PipelineHooks())
        ctx = make_ctx()
        audio = b"\x00\x01\x02"
        assert await executor.run_before_send_to_stt(audio, ctx) == audio

    async def test_hook_modifies_audio(self):
        def filter_hook(audio: bytes, ctx: HookContext) -> bytes:
            return audio + b"\xff"

        executor = PipelineHookExecutor(PipelineHooks(before_send_to_stt=filter_hook))
        result = await executor.run_before_send_to_stt(b"\x00", make_ctx())
        assert result == b"\x00\xff"

    async def test_hook_returns_none_drops_chunk(self):
        def drop_hook(audio: bytes, ctx: HookContext) -> bytes | None:
            return None

        executor = PipelineHookExecutor(PipelineHooks(before_send_to_stt=drop_hook))
        result = await executor.run_before_send_to_stt(b"\x00", make_ctx())
        assert result is None

    async def test_async_hook(self):
        async def async_hook(audio: bytes, ctx: HookContext) -> bytes:
            return audio * 2

        executor = PipelineHookExecutor(PipelineHooks(before_send_to_stt=async_hook))
        result = await executor.run_before_send_to_stt(b"ab", make_ctx())
        assert result == b"abab"

    async def test_hook_exception_fails_open(self, caplog):
        def bad_hook(audio: bytes, ctx: HookContext) -> bytes:
            raise ValueError("boom")

        executor = PipelineHookExecutor(PipelineHooks(before_send_to_stt=bad_hook))
        with caplog.at_level(logging.ERROR):
            result = await executor.run_before_send_to_stt(b"\x42", make_ctx())
        assert result == b"\x42"
        assert any("before_send_to_stt" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 14. Agent new fields (vad, audio_filter, background_audio)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestAgentNewFields:
    """Verify that the new optional fields on Agent default to None and
    accept values without breaking frozen semantics."""

    def test_agent_defaults_none(self):
        from patter.models import Agent
        agent = Agent(system_prompt="hi")
        assert agent.vad is None
        assert agent.audio_filter is None
        assert agent.background_audio is None

    def test_agent_accepts_new_fields(self):
        from patter.models import Agent
        sentinel_vad = object()
        sentinel_filter = object()
        sentinel_bg = object()
        agent = Agent(
            system_prompt="hi",
            vad=sentinel_vad,  # type: ignore[arg-type]
            audio_filter=sentinel_filter,  # type: ignore[arg-type]
            background_audio=sentinel_bg,  # type: ignore[arg-type]
        )
        assert agent.vad is sentinel_vad
        assert agent.audio_filter is sentinel_filter
        assert agent.background_audio is sentinel_bg


# ---------------------------------------------------------------------------
# 15. CallControl.send_dtmf
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestCallControlSendDtmf:
    """send_dtmf() dispatches to the injected _send_dtmf_fn; warns otherwise."""

    async def test_send_dtmf_warns_when_not_wired(self, caplog):
        from patter.models import CallControl
        cc = CallControl("c", "a", "b", "twilio")
        with caplog.at_level(logging.WARNING):
            await cc.send_dtmf("123")
        assert any("send_dtmf" in r.message for r in caplog.records)

    async def test_send_dtmf_dispatches_with_delay(self):
        from patter.models import CallControl
        calls: list[tuple[str, int]] = []

        async def fake_send(digits: str, delay_ms: int) -> None:
            calls.append((digits, delay_ms))

        cc = CallControl("c", "a", "b", "twilio", _send_dtmf_fn=fake_send)
        await cc.send_dtmf("1234#", delay_ms=500)
        assert calls == [("1234#", 500)]

    async def test_send_dtmf_default_delay(self):
        from patter.models import CallControl
        calls: list[tuple[str, int]] = []

        async def fake_send(digits: str, delay_ms: int) -> None:
            calls.append((digits, delay_ms))

        cc = CallControl("c", "a", "b", "twilio", _send_dtmf_fn=fake_send)
        await cc.send_dtmf("9")
        assert calls == [("9", 300)]
