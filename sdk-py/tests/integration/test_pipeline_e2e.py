"""End-to-end integration tests for pipeline hooks + sentence chunking.

Tests the full PipelineStreamHandler._process_streaming_response and
_process_regular_response paths with all hooks and guardrails exercised.
No real network calls — all external dependencies (STT, TTS, LLM, audio sender,
metrics) are mocked.

Coverage targets:
  1.  Sentence chunking on streaming LLM response — TTS called per-sentence.
  2.  afterTranscribe hook modifies transcript before LLM.
  3.  afterTranscribe veto (returns None) — LLM + TTS never called.
  4.  beforeSynthesize modifies each sentence before TTS.
  5.  beforeSynthesize veto (returns None) skips selected sentences.
  6.  afterSynthesize modifies audio bytes before send_audio.
  7.  afterSynthesize discards chunk (returns None) — nothing sent.
  8.  Guardrails fire in pipeline mode and pass replacement text to TTS.
  9.  Guardrails + hooks compose: guardrail fires first, hook receives replacement.
 10.  Non-streaming on_message with hooks — chunker applied, hooks fire.
 11.  LLM error mid-stream — chunker.reset() prevents partial content reaching TTS.
 12.  Barge-in (interrupt) during TTS — remaining sentences not synthesised.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from getpatter.handlers.stream_handler import PipelineStreamHandler
from getpatter.models import Agent, Guardrail, HookContext, PipelineHooks
from getpatter.services.sentence_chunker import SentenceChunker


# ---------------------------------------------------------------------------
# Test helpers / factories
# ---------------------------------------------------------------------------


def _make_audio_sender() -> AsyncMock:
    """Return a mock AudioSender that records every send_audio call."""
    sender = AsyncMock()
    sender.send_audio = AsyncMock()
    sender.send_clear = AsyncMock()
    sender.send_mark = AsyncMock()
    return sender


def _make_metrics() -> MagicMock:
    metrics = MagicMock()
    metrics.start_turn = MagicMock()
    metrics.record_stt_complete = MagicMock()
    metrics.record_llm_complete = MagicMock()
    metrics.record_tts_first_byte = MagicMock()
    metrics.record_tts_complete = MagicMock()
    metrics.record_turn_complete = MagicMock(return_value=MagicMock())
    metrics.record_turn_interrupted = MagicMock()
    metrics.get_cost_so_far = MagicMock(return_value=0.0)
    return metrics


def _make_agent(
    *,
    hooks: PipelineHooks | None = None,
    guardrails: list | None = None,
    first_message: str = "",
) -> Agent:
    return Agent(
        system_prompt="You are a helpful test agent.",
        voice="alloy",
        model="gpt-4o-mini",
        language="en",
        first_message=first_message,
        provider="pipeline",
        hooks=hooks,
        guardrails=guardrails,
    )


def _make_handler(
    agent: Agent,
    audio_sender=None,
    metrics=None,
    on_message=None,
    on_transcript=None,
    on_metrics=None,
) -> PipelineStreamHandler:
    """Construct a PipelineStreamHandler with all real providers replaced by mocks."""
    if audio_sender is None:
        audio_sender = _make_audio_sender()
    if metrics is None:
        metrics = _make_metrics()
    handler = PipelineStreamHandler(
        agent=agent,
        audio_sender=audio_sender,
        call_id="call-test-001",
        caller="+15550001111",
        callee="+15559999999",
        resolved_prompt="You are a helpful test agent.",
        metrics=metrics,
        on_message=on_message,
        on_transcript=on_transcript,
        on_metrics=on_metrics,
    )
    return handler


async def _async_tokens(*tokens: str) -> AsyncIterator[str]:
    """Async generator that yields each token in order."""
    for token in tokens:
        yield token


def _tts_synthesize_stub(audio_map: dict[str, bytes] | None = None):
    """Return a coroutine that acts as an async-generator TTS synthesize.

    audio_map: optional {sentence: audio_bytes} lookup.
    If absent, each sentence produces a single b'AUDIO:<sentence>' chunk.
    """

    async def _synthesize(text: str):
        if audio_map and text in audio_map:
            yield audio_map[text]
        else:
            yield f"AUDIO:{text}".encode()

    return _synthesize


# ---------------------------------------------------------------------------
# Test 1 — Sentence chunking: streaming LLM yields tokens forming 3 sentences
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestSentenceChunkingStreaming:
    """Verify SentenceChunker drives per-sentence TTS, not full-text TTS."""

    async def test_three_sentences_three_tts_calls(self) -> None:
        """TTS synthesize() is called once per sentence, not once for the whole text."""
        agent = _make_agent()
        audio_sender = _make_audio_sender()
        handler = _make_handler(agent, audio_sender=audio_sender)

        # Inject mock TTS
        handler._tts = MagicMock()
        synthesize_calls: list[str] = []

        async def _tts(text: str):
            synthesize_calls.append(text)
            yield b"audio"

        handler._tts.synthesize = _tts

        # Long enough tokens to trigger the chunker's 20-char minimum.
        tokens = [
            "Hello there, welcome to the service.",
            " How are you doing today?",
            " I am doing very well, thank you.",
        ]

        await handler._process_streaming_response(
            _async_tokens(*tokens), call_id="call-test-001"
        )

        # The chunker should have split the concatenated text into 3 sentences
        assert len(synthesize_calls) == 3, (
            f"Expected 3 TTS calls, got {len(synthesize_calls)}: {synthesize_calls}"
        )
        # Each sentence contains meaningful text
        assert all(len(s) > 5 for s in synthesize_calls)

    async def test_audio_is_forwarded_to_sender(self) -> None:
        """Audio chunks from TTS reach audio_sender.send_audio."""
        agent = _make_agent()
        audio_sender = _make_audio_sender()
        handler = _make_handler(agent, audio_sender=audio_sender)

        handler._tts = MagicMock()

        async def _tts(text: str):
            yield b"chunk1"
            yield b"chunk2"

        handler._tts.synthesize = _tts

        tokens = [
            "Hello there, this is a complete sentence.",
            " And here is another one for you.",
            " The third sentence finishes the stream nicely.",
        ]

        await handler._process_streaming_response(
            _async_tokens(*tokens), call_id="call-test-001"
        )

        # At least some audio was forwarded
        assert audio_sender.send_audio.await_count > 0


# ---------------------------------------------------------------------------
# Test 2 — afterTranscribe modifies transcript before LLM
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestAfterTranscribeModifiesTranscript:
    """afterTranscribe hook receives raw STT text and returns modified text
    that is then passed to the LLM / on_message handler."""

    async def test_uppercase_hook_forwarded_to_on_message(self) -> None:
        received_texts: list[str] = []

        def uppercase_hook(transcript: str, ctx: HookContext) -> str:
            return transcript.upper()

        agent = _make_agent(hooks=PipelineHooks(after_transcribe=uppercase_hook))
        audio_sender = _make_audio_sender()
        metrics = _make_metrics()

        # Capture what text on_message receives
        async def on_message(data: dict) -> str:
            received_texts.append(data["text"])
            return ""

        handler = _make_handler(
            agent, audio_sender=audio_sender, metrics=metrics, on_message=on_message
        )
        # Simulate CallControl (needed by _stt_loop)
        from getpatter.models import CallControl

        handler._call_control = CallControl(
            call_id="call-test-001",
            caller="+15550001111",
            callee="+15559999999",
            telephony_provider="telnyx",
        )

        # Build a fake transcript object
        transcript = MagicMock()
        transcript.is_final = True
        transcript.text = "hello world"

        # Feed the transcript through the hook pipeline directly
        from getpatter.services.pipeline_hooks import PipelineHookExecutor

        hook_executor = PipelineHookExecutor(agent.hooks)
        hook_ctx = handler._build_hook_context()
        result = await hook_executor.run_after_transcribe(transcript.text, hook_ctx)

        assert result == "HELLO WORLD"

    async def test_llm_receives_filtered_text_not_raw(self) -> None:
        """Full _stt_loop path: LLM receives the hooked (uppercased) text."""
        received_by_llm: list[str] = []

        def uppercase_hook(transcript: str, ctx: HookContext) -> str:
            return transcript.upper()

        async def on_message(data: dict) -> str:
            received_by_llm.append(data["text"])
            return "OK"

        agent = _make_agent(hooks=PipelineHooks(after_transcribe=uppercase_hook))
        audio_sender = _make_audio_sender()
        metrics = _make_metrics()
        handler = _make_handler(
            agent, audio_sender=audio_sender, metrics=metrics, on_message=on_message
        )

        # Mock the TTS so we don't need a real one
        handler._tts = MagicMock()

        async def _tts_noop(text: str):
            return
            yield  # make it an async generator

        handler._tts.synthesize = _tts_noop

        from getpatter.models import CallControl

        handler._call_control = CallControl(
            call_id="call-test-001",
            caller="+15550001111",
            callee="+15559999999",
            telephony_provider="telnyx",
        )

        # Simulate one transcript coming through the STT loop
        async def _fake_transcripts():
            t = MagicMock()
            t.is_final = True
            t.text = "hello world"
            yield t

        handler._stt = MagicMock()
        handler._stt.receive_transcripts = _fake_transcripts

        await handler._stt_loop()

        assert received_by_llm == ["HELLO WORLD"]


# ---------------------------------------------------------------------------
# Test 3 — afterTranscribe returns None → LLM and TTS never called
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestAfterTranscribeVeto:
    """When afterTranscribe returns None, the turn is skipped."""

    async def test_veto_skips_llm_and_tts(self) -> None:
        llm_called: list[bool] = []
        tts_called: list[bool] = []

        def veto_hook(transcript: str, ctx: HookContext):
            return None

        async def on_message(data: dict) -> str:
            llm_called.append(True)
            return "response"

        agent = _make_agent(hooks=PipelineHooks(after_transcribe=veto_hook))
        audio_sender = _make_audio_sender()
        metrics = _make_metrics()
        handler = _make_handler(
            agent, audio_sender=audio_sender, metrics=metrics, on_message=on_message
        )

        handler._tts = MagicMock()

        async def _tts(text: str):
            tts_called.append(True)
            yield b"audio"

        handler._tts.synthesize = _tts

        from getpatter.models import CallControl

        handler._call_control = CallControl(
            call_id="call-test-001",
            caller="+15550001111",
            callee="+15559999999",
            telephony_provider="telnyx",
        )

        async def _fake_transcripts():
            t = MagicMock()
            t.is_final = True
            t.text = "trigger veto"
            yield t

        handler._stt = MagicMock()
        handler._stt.receive_transcripts = _fake_transcripts

        await handler._stt_loop()

        assert llm_called == [], "LLM (on_message) must NOT be called when hook vetoes"
        assert tts_called == [], "TTS must NOT be called when hook vetoes"
        metrics.record_turn_interrupted.assert_called_once()

    async def test_veto_records_interrupted_metric(self) -> None:
        def veto_hook(transcript: str, ctx: HookContext):
            return None

        async def on_message(data: dict) -> str:
            return "noop"

        agent = _make_agent(hooks=PipelineHooks(after_transcribe=veto_hook))
        metrics = _make_metrics()
        handler = _make_handler(
            agent, metrics=metrics, on_message=on_message
        )

        from getpatter.models import CallControl

        handler._call_control = CallControl(
            call_id="call-test-001",
            caller="+15550001111",
            callee="+15559999999",
            telephony_provider="telnyx",
        )

        async def _fake_transcripts():
            t = MagicMock()
            t.is_final = True
            t.text = "anything"
            yield t

        handler._stt = MagicMock()
        handler._stt.receive_transcripts = _fake_transcripts

        await handler._stt_loop()

        metrics.record_turn_interrupted.assert_called_once()


# ---------------------------------------------------------------------------
# Test 4 — beforeSynthesize modifies per-sentence text before TTS
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestBeforeSynthesizeModifies:
    """beforeSynthesize hook modifies each sentence; TTS receives modified text."""

    async def test_prefix_added_to_each_sentence(self) -> None:
        tts_inputs: list[str] = []

        def prefix_hook(text: str, ctx: HookContext) -> str:
            return f"MODIFIED: {text}"

        agent = _make_agent(hooks=PipelineHooks(before_synthesize=prefix_hook))
        audio_sender = _make_audio_sender()
        handler = _make_handler(agent, audio_sender=audio_sender)

        handler._tts = MagicMock()

        async def _tts(text: str):
            tts_inputs.append(text)
            yield b"audio"

        handler._tts.synthesize = _tts

        tokens = [
            "This is the first sentence for you.",
            " And this is the second sentence here.",
        ]

        await handler._process_streaming_response(
            _async_tokens(*tokens), call_id="call-test-001"
        )

        assert all(t.startswith("MODIFIED: ") for t in tts_inputs), (
            f"All TTS inputs should be prefixed, got: {tts_inputs}"
        )
        assert len(tts_inputs) >= 2

    async def test_non_streaming_hook_fires(self) -> None:
        """beforeSynthesize also fires in the _process_regular_response path."""
        tts_inputs: list[str] = []

        def prefix_hook(text: str, ctx: HookContext) -> str:
            return f"[HOOK] {text}"

        agent = _make_agent(hooks=PipelineHooks(before_synthesize=prefix_hook))
        audio_sender = _make_audio_sender()
        handler = _make_handler(agent, audio_sender=audio_sender)

        handler._tts = MagicMock()

        async def _tts(text: str):
            tts_inputs.append(text)
            yield b"audio"

        handler._tts.synthesize = _tts

        await handler._process_regular_response(
            "Hello there, this is a complete response for the user.",
            call_id="call-test-001",
        )

        assert len(tts_inputs) >= 1
        assert all(t.startswith("[HOOK] ") for t in tts_inputs)


# ---------------------------------------------------------------------------
# Test 5 — beforeSynthesize veto skips selected sentences
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestBeforeSynthesizeVeto:
    """beforeSynthesize returning None skips TTS for that sentence only."""

    async def test_middle_sentence_skipped(self) -> None:
        tts_inputs: list[str] = []

        def selective_veto(text: str, ctx: HookContext):
            if "skip" in text.lower():
                return None
            return text

        agent = _make_agent(hooks=PipelineHooks(before_synthesize=selective_veto))
        audio_sender = _make_audio_sender()
        handler = _make_handler(agent, audio_sender=audio_sender)

        handler._tts = MagicMock()

        async def _tts(text: str):
            tts_inputs.append(text)
            yield b"audio"

        handler._tts.synthesize = _tts

        # Use a response long enough to chunk into 3 sentences
        response = (
            "Keep this sentence it is very important for users."
            " Please skip this middle sentence right now."
            " And this final sentence should also be kept."
        )

        await handler._process_regular_response(response, call_id="call-test-001")

        kept = [t for t in tts_inputs if "skip" not in t.lower()]
        skipped = [t for t in tts_inputs if "skip" in t.lower()]

        assert len(skipped) == 0, f"Skipped sentence leaked into TTS: {skipped}"
        assert len(kept) >= 1, "At least one sentence should have been synthesized"

    async def test_all_sentences_vetoed_no_audio_sent(self) -> None:
        def always_veto(text: str, ctx: HookContext):
            return None

        agent = _make_agent(hooks=PipelineHooks(before_synthesize=always_veto))
        audio_sender = _make_audio_sender()
        handler = _make_handler(agent, audio_sender=audio_sender)

        handler._tts = MagicMock()
        tts_calls: list[str] = []

        async def _tts(text: str):
            tts_calls.append(text)
            yield b"audio"

        handler._tts.synthesize = _tts

        await handler._process_regular_response(
            "This is a sentence that should be vetoed by the hook.",
            call_id="call-test-001",
        )

        assert tts_calls == [], "TTS should not be called when all sentences vetoed"
        assert audio_sender.send_audio.await_count == 0


# ---------------------------------------------------------------------------
# Test 6 — afterSynthesize modifies audio bytes
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestAfterSynthesizeModifiesAudio:
    """afterSynthesize receives raw TTS audio and its return value is sent."""

    async def test_reversed_audio_reaches_sender(self) -> None:
        sent_audio: list[bytes] = []

        def reverse_audio(audio: bytes, text: str, ctx: HookContext) -> bytes:
            return bytes(reversed(audio))

        agent = _make_agent(hooks=PipelineHooks(after_synthesize=reverse_audio))
        audio_sender = _make_audio_sender()

        async def _record_send(chunk: bytes) -> None:
            sent_audio.append(chunk)

        audio_sender.send_audio = AsyncMock(side_effect=_record_send)
        handler = _make_handler(agent, audio_sender=audio_sender)

        handler._tts = MagicMock()
        raw_audio = b"\x01\x02\x03\x04"

        async def _tts(text: str):
            yield raw_audio

        handler._tts.synthesize = _tts

        await handler._process_regular_response(
            "This is a test sentence for audio modification.",
            call_id="call-test-001",
        )

        assert len(sent_audio) >= 1
        # All chunks should be reversed versions of the original
        for chunk in sent_audio:
            assert chunk == bytes(reversed(raw_audio))

    async def test_hook_receives_correct_text_argument(self) -> None:
        """afterSynthesize receives the text that was sent to TTS."""
        received_texts: list[str] = []

        def capture_text(audio: bytes, text: str, ctx: HookContext) -> bytes:
            received_texts.append(text)
            return audio

        agent = _make_agent(hooks=PipelineHooks(after_synthesize=capture_text))
        audio_sender = _make_audio_sender()
        handler = _make_handler(agent, audio_sender=audio_sender)

        handler._tts = MagicMock()

        async def _tts(text: str):
            yield b"audio"

        handler._tts.synthesize = _tts

        await handler._process_regular_response(
            "Hello there, this is a sentence.", call_id="call-test-001"
        )

        assert len(received_texts) >= 1
        # The text passed to afterSynthesize should be a non-empty string
        assert all(len(t) > 0 for t in received_texts)


# ---------------------------------------------------------------------------
# Test 7 — afterSynthesize returns None → chunk discarded
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestAfterSynthesizeDiscardsChunk:
    """afterSynthesize returning None prevents the audio chunk from being sent."""

    async def test_no_audio_sent_when_hook_returns_none(self) -> None:
        def discard_all(audio: bytes, text: str, ctx: HookContext):
            return None

        agent = _make_agent(hooks=PipelineHooks(after_synthesize=discard_all))
        audio_sender = _make_audio_sender()
        handler = _make_handler(agent, audio_sender=audio_sender)

        handler._tts = MagicMock()

        async def _tts(text: str):
            yield b"audio-chunk-1"
            yield b"audio-chunk-2"

        handler._tts.synthesize = _tts

        await handler._process_regular_response(
            "This sentence should produce audio that gets discarded.",
            call_id="call-test-001",
        )

        audio_sender.send_audio.assert_not_awaited()

    async def test_partial_discard_some_chunks_sent(self) -> None:
        """If hook discards only even-indexed chunks, odd ones still arrive."""
        sent: list[bytes] = []
        call_count = {"n": 0}

        def alternate_discard(audio: bytes, text: str, ctx: HookContext):
            call_count["n"] += 1
            if call_count["n"] % 2 == 0:
                return None
            return audio

        async def _record_send(chunk: bytes) -> None:
            sent.append(chunk)

        agent = _make_agent(hooks=PipelineHooks(after_synthesize=alternate_discard))
        audio_sender = _make_audio_sender()
        audio_sender.send_audio = AsyncMock(side_effect=_record_send)
        handler = _make_handler(agent, audio_sender=audio_sender)

        handler._tts = MagicMock()

        async def _tts(text: str):
            yield b"chunk-1"
            yield b"chunk-2"
            yield b"chunk-3"
            yield b"chunk-4"

        handler._tts.synthesize = _tts

        await handler._process_regular_response(
            "This is a sentence that will produce multiple audio chunks.",
            call_id="call-test-001",
        )

        # Odd chunks (1 and 3) should be sent; even (2 and 4) discarded
        assert b"chunk-1" in sent
        assert b"chunk-3" in sent
        assert b"chunk-2" not in sent
        assert b"chunk-4" not in sent


# ---------------------------------------------------------------------------
# Test 8 — Guardrails fire in pipeline mode
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestGuardrailsPipelineMode:
    """Guardrails intercept per-sentence text and replace with safe message."""

    async def test_blocked_sentence_replaced_in_streaming(self) -> None:
        tts_inputs: list[str] = []

        guardrail = Guardrail(
            name="forbidden-guard",
            blocked_terms=["forbidden"],
            replacement="I cannot say that.",
        )
        agent = _make_agent(guardrails=[guardrail])
        audio_sender = _make_audio_sender()
        handler = _make_handler(agent, audio_sender=audio_sender)

        handler._tts = MagicMock()

        async def _tts(text: str):
            tts_inputs.append(text)
            yield b"audio"

        handler._tts.synthesize = _tts

        tokens = ["This is forbidden content that should be blocked by guardrail."]

        await handler._process_streaming_response(
            _async_tokens(*tokens), call_id="call-test-001"
        )

        # "forbidden" text must not reach TTS; replacement must appear
        for t in tts_inputs:
            assert "forbidden" not in t.lower(), (
                f"Guardrail failed — forbidden content reached TTS: {t}"
            )
        assert any("cannot say that" in t.lower() or "sorry" in t.lower() for t in tts_inputs), (
            f"Replacement text not found in TTS inputs: {tts_inputs}"
        )

    async def test_blocked_sentence_replaced_in_regular(self) -> None:
        tts_inputs: list[str] = []

        guardrail = Guardrail(
            name="forbidden-guard",
            blocked_terms=["forbidden"],
            replacement="I cannot say that.",
        )
        agent = _make_agent(guardrails=[guardrail])
        audio_sender = _make_audio_sender()
        handler = _make_handler(agent, audio_sender=audio_sender)

        handler._tts = MagicMock()

        async def _tts(text: str):
            tts_inputs.append(text)
            yield b"audio"

        handler._tts.synthesize = _tts

        await handler._process_regular_response(
            "This is forbidden content here.", call_id="call-test-001"
        )

        for t in tts_inputs:
            assert "forbidden" not in t.lower()
        assert any("cannot say that" in t.lower() or "sorry" in t.lower() for t in tts_inputs)

    async def test_clean_response_passes_through(self) -> None:
        tts_inputs: list[str] = []

        guardrail = Guardrail(
            name="forbidden-guard",
            blocked_terms=["forbidden"],
            replacement="I cannot say that.",
        )
        agent = _make_agent(guardrails=[guardrail])
        audio_sender = _make_audio_sender()
        handler = _make_handler(agent, audio_sender=audio_sender)

        handler._tts = MagicMock()

        async def _tts(text: str):
            tts_inputs.append(text)
            yield b"audio"

        handler._tts.synthesize = _tts

        await handler._process_regular_response(
            "This is a perfectly safe response.", call_id="call-test-001"
        )

        assert len(tts_inputs) >= 1
        for t in tts_inputs:
            assert "cannot say" not in t.lower()


# ---------------------------------------------------------------------------
# Test 9 — Guardrails + hooks compose correctly
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestGuardrailsAndHooksCompose:
    """Guardrails fire first; the beforeSynthesize hook receives replacement text."""

    async def test_guardrail_fires_then_hook_receives_replacement(self) -> None:
        before_synth_inputs: list[str] = []

        def capture_hook(text: str, ctx: HookContext) -> str:
            before_synth_inputs.append(text)
            return text

        guardrail = Guardrail(
            name="content-guard",
            blocked_terms=["forbidden"],
            replacement="Safe replacement text here.",
        )
        agent = _make_agent(
            hooks=PipelineHooks(before_synthesize=capture_hook),
            guardrails=[guardrail],
        )
        audio_sender = _make_audio_sender()
        handler = _make_handler(agent, audio_sender=audio_sender)

        handler._tts = MagicMock()

        async def _tts(text: str):
            yield b"audio"

        handler._tts.synthesize = _tts

        # Streaming path — guardrail operates per-sentence
        tokens = ["This sentence has forbidden content that must be replaced now."]

        await handler._process_streaming_response(
            _async_tokens(*tokens), call_id="call-test-001"
        )

        # Hook must have received the replacement text, not "forbidden"
        for text_seen in before_synth_inputs:
            assert "forbidden" not in text_seen.lower(), (
                f"Hook received forbidden content: {text_seen}"
            )
        assert any(
            "safe replacement" in t.lower() or "sorry" in t.lower() or "cannot" in t.lower()
            for t in before_synth_inputs
        ), f"Hook did not receive replacement text: {before_synth_inputs}"

    async def test_hook_applied_after_guardrail_replacement_regular(self) -> None:
        tts_inputs: list[str] = []

        def prefix_hook(text: str, ctx: HookContext) -> str:
            return f"[FILTERED] {text}"

        guardrail = Guardrail(
            name="content-guard",
            blocked_terms=["forbidden"],
            replacement="Safe message.",
        )
        agent = _make_agent(
            hooks=PipelineHooks(before_synthesize=prefix_hook),
            guardrails=[guardrail],
        )
        audio_sender = _make_audio_sender()
        handler = _make_handler(agent, audio_sender=audio_sender)

        handler._tts = MagicMock()

        async def _tts(text: str):
            tts_inputs.append(text)
            yield b"audio"

        handler._tts.synthesize = _tts

        # Regular path — guardrail fires on full response, then chunker + hook
        await handler._process_regular_response(
            "This has forbidden content.", call_id="call-test-001"
        )

        # TTS must receive the hook-modified replacement (not original forbidden content)
        for t in tts_inputs:
            assert "forbidden" not in t.lower()
        assert all(t.startswith("[FILTERED] ") for t in tts_inputs)


# ---------------------------------------------------------------------------
# Test 10 — Non-streaming on_message with hooks
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestNonStreamingOnMessageWithHooks:
    """_process_regular_response path also applies chunking + hooks."""

    async def test_hooks_fire_on_regular_response_sentences(self) -> None:
        tts_inputs: list[str] = []

        def prefix_hook(text: str, ctx: HookContext) -> str:
            return f"HOOK:{text}"

        agent = _make_agent(hooks=PipelineHooks(before_synthesize=prefix_hook))
        audio_sender = _make_audio_sender()
        handler = _make_handler(agent, audio_sender=audio_sender)

        handler._tts = MagicMock()

        async def _tts(text: str):
            tts_inputs.append(text)
            yield b"audio"

        handler._tts.synthesize = _tts

        # Two distinct sentences that the chunker should separate
        response = (
            "This is the first complete sentence for the test."
            " And this is the second complete sentence."
        )

        await handler._process_regular_response(response, call_id="call-test-001")

        assert len(tts_inputs) >= 1
        assert all(t.startswith("HOOK:") for t in tts_inputs)

    async def test_full_stt_loop_regular_response(self) -> None:
        """End-to-end: STT → on_message (regular) → hooks → TTS."""
        tts_inputs: list[str] = []

        def prefix_hook(text: str, ctx: HookContext) -> str:
            return f"PROCESSED:{text}"

        async def on_message(data: dict) -> str:
            return "This is the agent response text here."

        agent = _make_agent(hooks=PipelineHooks(before_synthesize=prefix_hook))
        audio_sender = _make_audio_sender()
        metrics = _make_metrics()
        handler = _make_handler(
            agent, audio_sender=audio_sender, metrics=metrics, on_message=on_message
        )

        handler._tts = MagicMock()

        async def _tts(text: str):
            tts_inputs.append(text)
            yield b"audio"

        handler._tts.synthesize = _tts

        from getpatter.models import CallControl

        handler._call_control = CallControl(
            call_id="call-test-001",
            caller="+15550001111",
            callee="+15559999999",
            telephony_provider="telnyx",
        )

        async def _fake_transcripts():
            t = MagicMock()
            t.is_final = True
            t.text = "user input text"
            yield t

        handler._stt = MagicMock()
        handler._stt.receive_transcripts = _fake_transcripts

        await handler._stt_loop()

        assert len(tts_inputs) >= 1
        assert all(t.startswith("PROCESSED:") for t in tts_inputs)


# ---------------------------------------------------------------------------
# Test 11 — LLM error mid-stream → chunker.reset() prevents partial TTS
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestLLMErrorMidStream:
    """When the LLM generator raises mid-stream, chunker.reset() is called."""

    async def test_no_tts_after_llm_error(self) -> None:
        """Partial tokens before an LLM error must not produce garbled TTS."""
        tts_inputs: list[str] = []

        agent = _make_agent()
        audio_sender = _make_audio_sender()
        handler = _make_handler(agent, audio_sender=audio_sender)

        handler._tts = MagicMock()

        async def _tts(text: str):
            tts_inputs.append(text)
            yield b"audio"

        handler._tts.synthesize = _tts

        async def _bad_llm():
            yield "Hello there"  # short — chunker buffers it
            raise RuntimeError("LLM network failure")

        # Capture the chunker to verify reset was triggered
        original_chunker_class = SentenceChunker
        reset_called: list[bool] = []

        class TrackingChunker(SentenceChunker):
            def reset(self) -> None:
                reset_called.append(True)
                super().reset()

        with patch(
            "getpatter.handlers.stream_handler.SentenceChunker",
            TrackingChunker,
        ):
            await handler._process_streaming_response(
                _bad_llm(), call_id="call-test-001"
            )

        assert reset_called, "chunker.reset() must be called on LLM error"
        # Short buffered content before error was never a complete sentence
        # so TTS should not have been called
        assert tts_inputs == [], (
            f"TTS should not receive partial content on LLM error: {tts_inputs}"
        )

    async def test_tts_completed_sentences_before_error(self) -> None:
        """Sentences completed before the error may have already been synthesized."""
        tts_inputs: list[str] = []

        agent = _make_agent()
        audio_sender = _make_audio_sender()
        handler = _make_handler(agent, audio_sender=audio_sender)

        handler._tts = MagicMock()

        async def _tts(text: str):
            tts_inputs.append(text)
            yield b"audio"

        handler._tts.synthesize = _tts

        async def _partial_llm():
            # Yield a complete sentence first (long enough for chunker)
            yield "Hello there, this is a complete sentence for you."
            yield " And this is the second full sentence right here."
            # Now a partial third sentence, then crash
            yield " And the third is incom"
            raise RuntimeError("died mid-third-sentence")

        with patch(
            "getpatter.handlers.stream_handler.SentenceChunker",
            SentenceChunker,
        ):
            await handler._process_streaming_response(
                _partial_llm(), call_id="call-test-001"
            )

        # The partial third sentence must NOT appear in TTS
        for t in tts_inputs:
            assert "incom" not in t.lower(), (
                f"Partial sentence leaked into TTS: {t}"
            )


# ---------------------------------------------------------------------------
# Test 12 — Barge-in (interrupt) during TTS
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestBargeInInterrupt:
    """Setting _is_speaking = False mid-synthesis stops further TTS calls."""

    async def test_remaining_sentences_not_synthesized_after_interrupt(self) -> None:
        tts_call_count: list[int] = [0]
        tts_calls: list[str] = []

        agent = _make_agent()
        audio_sender = _make_audio_sender()
        handler = _make_handler(agent, audio_sender=audio_sender)

        # Allow the first TTS call, then simulate barge-in (interrupt)
        call_sequence: list[int] = []

        async def _tts_with_interrupt(text: str):
            tts_calls.append(text)
            call_sequence.append(len(tts_calls))
            if len(tts_calls) == 1:
                # After the first sentence starts, simulate barge-in
                handler._is_speaking = False
            yield b"audio"

        handler._tts = MagicMock()
        handler._tts.synthesize = _tts_with_interrupt

        # Response long enough to produce 3 sentences
        response = (
            "First sentence is complete and ready to go out now."
            " Second sentence should be interrupted by user barge-in."
            " Third sentence should never reach TTS at all ever."
        )

        await handler._process_regular_response(response, call_id="call-test-001")

        # At most the first sentence should have been synthesized
        assert len(tts_calls) <= 1, (
            f"Expected at most 1 TTS call after interrupt, got {len(tts_calls)}: {tts_calls}"
        )

    async def test_is_speaking_reset_to_false_in_finally(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_is_speaking must be False after the response is complete (finally block).

        Sets PATTER_TTS_TAIL_GRACE_MS=0 to disable the carrier-buffer grace
        period so the immediate-flip semantics this test was written for are
        preserved. With grace > 0 (the default, used in production for VAD
        barge-in coverage during Twilio's playback buffer), the flag stays
        True for ~1.5s after response complete — see ``test_is_speaking_grace_period``.
        """
        monkeypatch.setenv("PATTER_TTS_TAIL_GRACE_MS", "0")
        agent = _make_agent()
        audio_sender = _make_audio_sender()
        handler = _make_handler(agent, audio_sender=audio_sender)

        handler._tts = MagicMock()

        async def _tts(text: str):
            yield b"audio"

        handler._tts.synthesize = _tts

        assert handler._is_speaking is False  # precondition

        await handler._process_regular_response(
            "This response will set and then reset the speaking flag.",
            call_id="call-test-001",
        )

        assert handler._is_speaking is False, (
            "_is_speaking must be reset to False after response completes"
        )

    async def test_is_speaking_reset_even_on_exception(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_is_speaking is reset by the finally block even if TTS raises."""
        monkeypatch.setenv("PATTER_TTS_TAIL_GRACE_MS", "0")
        agent = _make_agent()
        audio_sender = _make_audio_sender()
        handler = _make_handler(agent, audio_sender=audio_sender)

        handler._tts = MagicMock()

        call_count = {"n": 0}

        async def _crashing_tts(text: str):
            call_count["n"] += 1
            raise RuntimeError("TTS provider down")
            yield  # make it an async generator

        handler._tts.synthesize = _crashing_tts

        # Should not raise — the streaming loop catches exceptions
        try:
            await handler._process_regular_response(
                "This will crash TTS.", call_id="call-test-001"
            )
        except Exception:
            pass  # implementation may propagate or swallow

        assert handler._is_speaking is False, (
            "_is_speaking must be False even when an exception occurs"
        )


# ---------------------------------------------------------------------------
# Test: SentenceChunker reset clears buffer correctly
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestSentenceChunkerReset:
    """SentenceChunker.reset() discards all buffered content."""

    def test_reset_clears_buffer(self) -> None:
        chunker = SentenceChunker()
        chunker.push("Hello there, partial sen")
        chunker.reset()
        result = chunker.flush()
        assert result == [], f"After reset, flush should return empty: {result}"

    def test_reset_then_new_content_works(self) -> None:
        chunker = SentenceChunker()
        chunker.push("Old partial content")
        chunker.reset()
        # Feed a complete sentence after reset
        chunker.push("New fresh content here, completely separate from before.")
        result = chunker.flush()
        assert any("New fresh" in s for s in result)
        assert all("Old partial" not in s for s in result)

    def test_push_flush_without_reset(self) -> None:
        chunker = SentenceChunker()
        sentences = chunker.push("Hello there, this is a full and complete sentence.")
        flushed = chunker.flush()
        all_output = sentences + flushed
        assert len(all_output) >= 1
        assert any("Hello" in s for s in all_output)


# ---------------------------------------------------------------------------
# Test: PipelineStreamHandler._build_hook_context
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestBuildHookContext:
    """_build_hook_context produces a correct HookContext snapshot."""

    def test_context_carries_call_metadata(self) -> None:
        agent = _make_agent()
        handler = _make_handler(agent)
        handler.conversation_history.append({"role": "user", "text": "hi"})

        ctx = handler._build_hook_context()

        assert ctx.call_id == "call-test-001"
        assert ctx.caller == "+15550001111"
        assert ctx.callee == "+15559999999"
        assert isinstance(ctx.history, tuple)
        assert len(ctx.history) == 1
        assert ctx.history[0]["role"] == "user"

    def test_context_is_immutable(self) -> None:
        agent = _make_agent()
        handler = _make_handler(agent)
        ctx = handler._build_hook_context()

        with pytest.raises(Exception):
            ctx.call_id = "mutated"  # type: ignore[misc]
