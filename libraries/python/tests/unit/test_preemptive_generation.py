"""PREEMPTIVE GENERATION (pipeline mode, opt-in): start the LLM + TTS early on
a confident INTERIM transcript, hold the audio, and commit-or-discard when the
FINAL transcript arrives.

Covers the full speculation lifecycle against the real ``PipelineStreamHandler``
turn machinery — only the external boundary (LLM stream, TTS bytes, STT
transcripts, VAD events) is faked:

* speculation starts on a punctuated interim immediately, and on a
  non-punctuated interim once it has been stable ``preemptive_min_stable_ms``;
* a matching final RELEASES the speculation: buffered audio flushes to the
  carrier, exactly one user history entry (final transcript text) + one
  assistant entry + one metrics turn are recorded, and the hit counter ticks;
* a mismatched final DISCARDS the speculation (no history pollution, miss
  counter ticks) and the normal dispatch answers the final;
* VAD speech_start during speculation aborts it silently (nothing audible);
* a newer qualifying interim replaces the in-flight speculation;
* buffer overflow (> ~15 s of held audio) aborts the speculation.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from getpatter.providers.base import Transcript, VADEvent
from getpatter.stream_handler import (
    _PREEMPTIVE_MAX_BUFFER_S,
    PipelineStreamHandler,
    _ends_with_sentence_final_punct,
    _speculation_matches,
)

from tests.conftest import make_agent

_FRAME = b"\x00\x01" * 160


class _FakeTTS:
    """Yields one fixed PCM chunk per sentence and records what it spoke."""

    output_format = "pcm_16000"

    def __init__(self, chunk: bytes = b"\x00\x01" * 160) -> None:
        self.synthesized: list[str] = []
        self._chunk = chunk

    async def synthesize(self, text: str):
        self.synthesized.append(text)
        yield self._chunk

    async def close(self) -> None:  # pragma: no cover - teardown shim
        pass


class _ScriptedLLM:
    """Fake ``LLMLoop``: records every ``run`` call and streams a scripted
    reply token-by-token, honouring ``cancel_event``."""

    def __init__(self, replies: list[str] | None = None, delay_s: float = 0.0) -> None:
        self.calls: list[dict] = []
        self._replies = replies or ["Va bene, perfetto. "]
        self._delay_s = delay_s

    def run(self, text, history, ctx, *, cancel_event=None, **kw):
        index = len(self.calls)
        self.calls.append({"text": text, "history": list(history)})
        reply = self._replies[min(index, len(self._replies) - 1)]

        async def _gen():
            if self._delay_s:
                await asyncio.sleep(self._delay_s)
            for token in reply.split(" "):
                if cancel_event is not None and cancel_event.is_set():
                    return
                yield token + " "

        return _gen()


class _ParkUntilCancelLLM:
    """First-token never arrives until the cancel event fires (models a slow
    agent-runtime turn parked pre-first-token)."""

    def __init__(self) -> None:
        self.calls = 0
        self.cancelled = asyncio.Event()

    def run(self, text, history, ctx, *, cancel_event=None, **kw):
        self.calls += 1

        async def _gen():
            try:
                while cancel_event is None or not cancel_event.is_set():
                    await asyncio.sleep(0.005)
            finally:
                # Records teardown whether the consumer observed the cancel
                # event or hard-cancelled the pending token fetch.
                self.cancelled.set()
            return
            yield  # pragma: no cover — makes this an async generator

        return _gen()


class _ScriptedVAD:
    def __init__(self, events: list[VADEvent | None]) -> None:
        self._events = list(events)

    async def process_frame(self, pcm: bytes, sample_rate: int) -> VADEvent | None:
        return self._events.pop(0) if self._events else None

    async def close(self) -> None:  # pragma: no cover
        pass

    def reset(self) -> None:
        pass


def _make_handler(
    *,
    metrics: MagicMock | None = None,
    llm: object | None = None,
    tts: _FakeTTS | None = None,
    **agent_overrides,
) -> PipelineStreamHandler:
    agent_kwargs = {"preemptive_generation": True, "preemptive_min_stable_ms": 30}
    agent_kwargs.update(agent_overrides)
    handler = PipelineStreamHandler(
        agent=make_agent(**agent_kwargs),
        audio_sender=AsyncMock(),
        call_id="call-preemptive",
        caller="+15551110000",
        callee="+15552220000",
        resolved_prompt="p",
        metrics=metrics if metrics is not None else MagicMock(),
        for_twilio=True,
        on_transcript=None,
        conversation_history=deque(maxlen=20),
        transcript_entries=deque(maxlen=20),
    )
    handler.on_message = None
    handler._tts = tts if tts is not None else _FakeTTS()  # type: ignore[assignment]
    handler._stt = AsyncMock()
    handler._aec = None
    handler._input_is_mulaw_8k = False
    handler._llm_loop = llm if llm is not None else _ScriptedLLM()
    # The audio_sender AsyncMock has no ``_input_is_mulaw_8k`` attr — give the
    # playback math the PCM16 16 kHz rate explicitly.
    handler.audio_sender._input_is_mulaw_8k = False
    return handler


async def _settle(handler: PipelineStreamHandler) -> None:
    """Drain any in-flight speculation/dispatch so the test exits cleanly."""
    await handler._abort_speculation(reason="test_teardown", count_miss=False)
    await handler._await_dispatch_settle()
    handler._cancel_interim_stability_task()
    handler._clear_grace_task()


@pytest.mark.unit
class TestHelpers:
    def test_sentence_final_punct(self) -> None:
        assert _ends_with_sentence_final_punct("che ore sono?")
        assert _ends_with_sentence_final_punct("prenota un tavolo. ")
        assert not _ends_with_sentence_final_punct("prenota un tavolo per")
        assert not _ends_with_sentence_final_punct("")

    def test_speculation_matches_normalises(self) -> None:
        assert _speculation_matches("what time is it", "What time is it?")
        assert _speculation_matches("ciao, come stai?", "ciao come stai")
        assert not _speculation_matches("what time", "what time is the meeting")
        assert not _speculation_matches("", "")


@pytest.mark.unit
@pytest.mark.asyncio
class TestSpeculationStart:
    async def test_disabled_by_default(self) -> None:
        handler = _make_handler()
        handler._preemptive_enabled = False  # simulate the Agent default
        await handler._note_interim_transcript("ciao come stai?")
        assert handler._speculation is None

    async def test_agent_default_is_off(self) -> None:
        agent = make_agent()
        assert agent.preemptive_generation is False
        assert agent.preemptive_min_stable_ms == 300

    async def test_punctuated_interim_starts_immediately(self) -> None:
        # Huge stability window: only the punctuation fast-path can fire.
        llm = _ScriptedLLM(delay_s=5.0)
        handler = _make_handler(llm=llm, preemptive_min_stable_ms=60_000)
        await handler._note_interim_transcript("ciao come stai?")
        assert handler._speculation is not None
        assert handler._speculation.interim_text == "ciao come stai?"
        await asyncio.sleep(0)  # let the task issue the LLM call
        assert len(llm.calls) == 1
        assert llm.calls[0]["text"] == "ciao come stai?"
        await _settle(handler)

    async def test_stable_interim_starts_after_window(self) -> None:
        llm = _ScriptedLLM(delay_s=5.0)
        handler = _make_handler(llm=llm, preemptive_min_stable_ms=30)
        await handler._note_interim_transcript("prenota un tavolo per due")
        assert handler._speculation is None  # no punctuation → not yet
        await asyncio.sleep(0.1)  # > preemptive_min_stable_ms
        assert handler._speculation is not None
        assert handler._speculation.interim_text == "prenota un tavolo per due"
        await _settle(handler)

    async def test_changed_interim_restarts_stability_window(self) -> None:
        handler = _make_handler(llm=_ScriptedLLM(delay_s=5.0), preemptive_min_stable_ms=80)
        await handler._note_interim_transcript("prenota un")
        await asyncio.sleep(0.04)
        await handler._note_interim_transcript("prenota un tavolo")  # text grew
        await asyncio.sleep(0.04)  # 80 ms since FIRST, 40 ms since latest
        assert handler._speculation is None
        await asyncio.sleep(0.08)
        assert handler._speculation is not None
        await _settle(handler)

    async def test_hallucination_interim_never_speculates(self) -> None:
        handler = _make_handler()
        await handler._note_interim_transcript("thank you for watching!")
        await asyncio.sleep(0.1)
        assert handler._speculation is None
        await _settle(handler)

    async def test_no_speculation_while_dispatch_in_flight(self) -> None:
        handler = _make_handler()
        handler._dispatch_task = asyncio.create_task(asyncio.sleep(0.3))
        await handler._note_interim_transcript("ciao come stai?")
        assert handler._speculation is None
        handler._dispatch_task.cancel()
        await _settle(handler)

    async def test_no_speculation_while_agent_speaking(self) -> None:
        handler = _make_handler()
        handler._is_speaking = True
        await handler._note_interim_transcript("ciao come stai?")
        assert handler._speculation is None
        handler._is_speaking = False
        await _settle(handler)


@pytest.mark.unit
@pytest.mark.asyncio
class TestReleaseOnMatchingFinal:
    async def test_full_loop_release_flushes_audio_single_turn(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv("PATTER_TTS_TAIL_GRACE_MS", "0")
        metrics = MagicMock()
        llm = _ScriptedLLM(replies=["Sono le tre del pomeriggio. "], delay_s=0.01)
        tts = _FakeTTS()
        handler = _make_handler(metrics=metrics, llm=llm, tts=tts)
        transcripts: list[dict] = []

        async def _on_transcript(payload: dict) -> None:
            transcripts.append(payload)

        handler.on_transcript = _on_transcript

        class _STT:
            async def receive_transcripts(self) -> AsyncIterator[Transcript]:
                # Confident interim (sentence-final punctuation) → speculate.
                yield Transcript(text="che ore sono?", is_final=False, confidence=0.9)
                # Let the speculative LLM+TTS run and buffer while the user's
                # end-of-utterance silence elapses. Nothing audible yet.
                await asyncio.sleep(0.15)
                assert handler.audio_sender.send_audio.await_count == 0
                assert handler._speculation is not None
                # The final matches (only punctuation/case differ) → RELEASE.
                yield Transcript(text="Che ore sono?", is_final=True, confidence=0.9)
                await asyncio.sleep(0.05)

        handler._stt = _STT()  # type: ignore[assignment]
        await asyncio.wait_for(handler._stt_loop(), timeout=3.0)

        # The LLM ran exactly once — on the interim.
        assert len(llm.calls) == 1
        assert llm.calls[0]["text"] == "che ore sono?"
        # Buffered audio was flushed to the carrier on release.
        assert handler.audio_sender.send_audio.await_count >= 1
        assert tts.synthesized == ["Sono le tre del pomeriggio."]
        # Exactly ONE user history entry — with the FINAL transcript text —
        # and one assistant entry.
        user_entries = [
            e for e in handler.conversation_history if e["role"] == "user"
        ]
        assistant_entries = [
            e for e in handler.conversation_history if e["role"] == "assistant"
        ]
        assert [e["text"] for e in user_entries] == ["Che ore sono?"]
        assert len(assistant_entries) == 1
        assert "Sono le tre" in assistant_entries[0]["text"]
        # on_transcript fired once per role, user line carries the final text.
        roles = [(t["role"], t["text"]) for t in transcripts]
        assert sum(1 for r, _ in roles if r == "user") == 1
        assert roles[0] == ("user", "Che ore sono?")
        # Metrics: one turn, recorded from the REAL commit point; hit counted.
        metrics.record_preemptive_hit.assert_called_once()
        metrics.record_preemptive_miss.assert_not_called()
        metrics.record_stt_complete.assert_called_once_with("Che ore sono?")
        metrics.record_turn_committed.assert_called_once()
        metrics.record_turn_complete.assert_called_once()
        metrics.record_llm_first_token.assert_called()
        metrics.record_tts_first_byte.assert_called()
        metrics.record_turn_interrupted.assert_not_called()

    async def test_release_while_llm_still_streaming(self, monkeypatch) -> None:
        """Final commits while the speculative LLM is paused MID-STREAM — the
        buffered first sentence flushes immediately at release (without
        waiting for the next token), and the rest streams live."""
        monkeypatch.setenv("PATTER_TTS_TAIL_GRACE_MS", "0")

        class _GatedLLM:
            """One long sentence, then the stream holds until ``gate``."""

            def __init__(self) -> None:
                self.calls: list[dict] = []
                self.gate = asyncio.Event()

            def run(self, text, history, ctx, *, cancel_event=None, **kw):
                self.calls.append({"text": text})
                gate = self.gate

                async def _gen():
                    yield "La prima frase della risposta è già abbastanza lunga. "
                    # A follow-up token confirms the sentence boundary so the
                    # chunker emits sentence 1 (it holds a terminator until
                    # the next token rules out decimals/abbreviations).
                    yield "E"
                    await gate.wait()
                    yield " la seconda frase arriva in diretta dopo il rilascio. "

                return _gen()

        llm = _GatedLLM()
        handler = _make_handler(llm=llm)

        class _STT:
            async def receive_transcripts(self) -> AsyncIterator[Transcript]:
                yield Transcript(text="dimmi due cose?", is_final=False, confidence=0.9)
                await asyncio.sleep(0.1)
                # Sentence 1 synthesized and HELD — nothing audible yet.
                assert handler.audio_sender.send_audio.await_count == 0
                yield Transcript(text="Dimmi due cose?", is_final=True, confidence=0.9)
                await asyncio.sleep(0.1)
                # Release flushed the buffered sentence EVEN THOUGH the LLM
                # is still parked mid-stream (no second token yet).
                assert handler.audio_sender.send_audio.await_count == 1
                llm.gate.set()
                await asyncio.sleep(0.1)

        handler._stt = _STT()  # type: ignore[assignment]
        await asyncio.wait_for(handler._stt_loop(), timeout=3.0)

        assert len(llm.calls) == 1
        # Sentence 2 streamed live after the gate opened.
        assert handler.audio_sender.send_audio.await_count >= 2
        assistant = [
            e for e in handler.conversation_history if e["role"] == "assistant"
        ]
        assert len(assistant) == 1
        assert "la seconda frase" in assistant[0]["text"]


@pytest.mark.unit
@pytest.mark.asyncio
class TestDiscardOnMismatchedFinal:
    async def test_full_loop_mismatch_dispatches_normally(self, monkeypatch) -> None:
        monkeypatch.setenv("PATTER_TTS_TAIL_GRACE_MS", "0")
        metrics = MagicMock()
        llm = _ScriptedLLM(
            replies=["RISPOSTA SPECULATIVA. ", "RISPOSTA REALE. "], delay_s=0.01
        )
        tts = _FakeTTS()
        handler = _make_handler(metrics=metrics, llm=llm, tts=tts)

        class _STT:
            async def receive_transcripts(self) -> AsyncIterator[Transcript]:
                yield Transcript(
                    text="prenota un tavolo.", is_final=False, confidence=0.9
                )
                await asyncio.sleep(0.15)  # speculation completes and parks
                # Final adds real words → mismatch → discard + normal dispatch.
                yield Transcript(
                    text="prenota un tavolo per quattro.",
                    is_final=True,
                    confidence=0.9,
                )
                await asyncio.sleep(0.1)

        handler._stt = _STT()  # type: ignore[assignment]
        await asyncio.wait_for(handler._stt_loop(), timeout=3.0)

        # Speculative + real dispatch both hit the LLM; the real one used the
        # FINAL transcript.
        assert [c["text"] for c in llm.calls] == [
            "prenota un tavolo.",
            "prenota un tavolo per quattro.",
        ]
        # Only the REAL turn's audio reached the carrier (speculation held +
        # discarded its buffer silently).
        assert handler.audio_sender.send_audio.await_count == 1
        assert tts.synthesized == ["RISPOSTA SPECULATIVA.", "RISPOSTA REALE."]
        # No duplicate history: one user entry (final text), one assistant.
        user_entries = [
            e for e in handler.conversation_history if e["role"] == "user"
        ]
        assistant_entries = [
            e for e in handler.conversation_history if e["role"] == "assistant"
        ]
        assert [e["text"] for e in user_entries] == [
            "prenota un tavolo per quattro."
        ]
        assert len(assistant_entries) == 1
        assert "REALE" in assistant_entries[0]["text"]
        metrics.record_preemptive_miss.assert_called_once()
        metrics.record_preemptive_hit.assert_not_called()
        metrics.record_turn_complete.assert_called_once()

    async def test_speculative_history_snapshot_excludes_uncommitted_turn(
        self,
    ) -> None:
        """The speculative prompt is built from a SNAPSHOT — the shared
        conversation_history is never touched before release."""
        llm = _ScriptedLLM(delay_s=5.0)
        handler = _make_handler(llm=llm, preemptive_min_stable_ms=60_000)
        handler.conversation_history.append(
            {"role": "assistant", "text": "Pronto?", "timestamp": time.time()}
        )
        await handler._note_interim_transcript("ciao come stai?")
        await asyncio.sleep(0.02)
        assert len(llm.calls) == 1
        # Snapshot = prior history + the speculated user entry.
        assert [e["role"] for e in llm.calls[0]["history"]] == ["assistant", "user"]
        assert llm.calls[0]["history"][-1]["text"] == "ciao come stai?"
        # Shared history untouched (no user entry committed yet).
        assert [e["role"] for e in handler.conversation_history] == ["assistant"]
        await _settle(handler)


@pytest.mark.unit
@pytest.mark.asyncio
class TestAbortPaths:
    async def test_vad_speech_start_aborts_silently(self) -> None:
        metrics = MagicMock()
        llm = _ParkUntilCancelLLM()
        handler = _make_handler(metrics=metrics, llm=llm)
        handler._auto_vad = _ScriptedVAD([VADEvent(type="speech_start")])
        await handler._note_interim_transcript("aspetta un attimo?")
        assert handler._speculation is not None
        spec = handler._speculation
        # Let the speculative task actually park on its (slow) LLM stream.
        await asyncio.sleep(0.02)

        # The user resumes speaking → VAD speech_start while the agent idles.
        await handler.on_audio_received(_FRAME)

        assert handler._speculation is None
        assert spec.cancel_event.is_set()
        assert llm.cancelled.is_set()  # the LLM stream was torn down
        # Silent: nothing was sent, nothing recorded in history.
        handler.audio_sender.send_audio.assert_not_called()
        handler.audio_sender.send_clear.assert_not_called()
        assert len(handler.conversation_history) == 0
        metrics.record_preemptive_miss.assert_called_once()
        metrics.record_turn_interrupted.assert_not_called()
        await _settle(handler)

    async def test_newer_qualifying_interim_replaces_speculation(self) -> None:
        metrics = MagicMock()
        llm = _ScriptedLLM(delay_s=5.0)
        handler = _make_handler(metrics=metrics, llm=llm, preemptive_min_stable_ms=60_000)
        await handler._note_interim_transcript("prenota un tavolo?")
        first = handler._speculation
        assert first is not None

        await handler._note_interim_transcript("prenota un tavolo per due?")
        second = handler._speculation
        assert second is not None and second is not first
        assert first.cancel_event.is_set()
        assert second.interim_text == "prenota un tavolo per due?"
        await asyncio.sleep(0.02)
        assert len(llm.calls) == 2
        metrics.record_preemptive_miss.assert_called_once()  # the replaced one
        await _settle(handler)

    async def test_same_interim_does_not_restart_speculation(self) -> None:
        llm = _ScriptedLLM(delay_s=5.0)
        handler = _make_handler(llm=llm, preemptive_min_stable_ms=60_000)
        await handler._note_interim_transcript("ciao come stai?")
        first = handler._speculation
        # Deepgram re-emits the same interim — keep the existing speculation.
        await handler._note_interim_transcript("Ciao, come stai?")
        assert handler._speculation is first
        await asyncio.sleep(0.02)
        assert len(llm.calls) == 1
        await _settle(handler)

    async def test_buffer_overflow_aborts_speculation(self) -> None:
        metrics = MagicMock()
        # One chunk per sentence, each chunk > 15 s of PCM16 @ 16 kHz.
        overflow_bytes = int((_PREEMPTIVE_MAX_BUFFER_S + 1.0) * 32_000)
        tts = _FakeTTS(chunk=b"\x00" * overflow_bytes)
        handler = _make_handler(metrics=metrics, tts=tts)
        await handler._note_interim_transcript("raccontami una storia lunga?")
        spec = handler._speculation
        assert spec is not None
        await asyncio.sleep(0.1)
        # The speculation self-aborted on overflow: deregistered + miss.
        assert handler._speculation is None
        assert spec.failed
        handler.audio_sender.send_audio.assert_not_called()
        metrics.record_preemptive_miss.assert_called_once()
        await _settle(handler)

    async def test_cleanup_tears_down_speculation_without_miss(self) -> None:
        metrics = MagicMock()
        handler = _make_handler(metrics=metrics, llm=_ParkUntilCancelLLM())
        await handler._note_interim_transcript("aspetta un secondo?")
        assert handler._speculation is not None
        handler._stt = AsyncMock()
        await handler.cleanup()
        assert handler._speculation is None
        metrics.record_preemptive_miss.assert_not_called()

    async def test_concurrent_start_keeps_newer_speculation(self) -> None:
        """A speculation registered while ``_start_speculation`` awaits the
        old one's unwind must win over the awaiting (older-text) one —
        overwriting it would orphan the newer task parked on its commit
        decision forever (held audio + open LLM stream, no miss counted)."""
        handler = _make_handler(llm=_ParkUntilCancelLLM())
        await handler._note_interim_transcript("la prima frase?")
        first = handler._speculation
        assert first is not None

        # Replace FIRST with "seconda"; while that replacement awaits FIRST's
        # task unwind, a newer interim ("terza") registers concurrently — the
        # same interleaving the stability watcher can produce against the
        # STT loop.
        replace_task = asyncio.create_task(
            handler._start_speculation("la seconda frase?")
        )
        await asyncio.sleep(0)  # replacement deregisters FIRST, parks on its unwind
        assert handler._speculation is None
        await handler._note_interim_transcript("la terza frase?")
        newest = handler._speculation
        assert newest is not None
        assert newest.interim_text == "la terza frase?"

        await asyncio.wait_for(replace_task, timeout=3.0)
        # The resuming replacement yielded to the newer registration instead
        # of overwriting it.
        assert handler._speculation is newest
        await _settle(handler)
