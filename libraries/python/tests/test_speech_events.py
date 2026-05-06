"""Unit tests for the speech-edge events dispatcher.

Tests run synthetically — no real PSTN, no real provider WebSocket. We drive
:class:`SpeechEvents` directly with the methods the SDK calls internally
(`fire_user_speech_started`, etc.) and assert the documented payload schema
plus state-machine and OTel attach behaviour.

Maps onto the test contract in ``docs/PROMPT_speech_events.md`` (12 cases).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from getpatter import Patter, SpeechEvents


# Helper: collect every payload the dispatcher fires.
class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def make(self, name: str):
        async def _cb(payload: dict[str, Any]) -> None:
            self.calls.append((name, dict(payload)))

        return _cb


@pytest.mark.unit
class TestUserSpeechEdges:
    async def test_user_speech_started_fires_on_vad_positive_edge(self) -> None:
        events = SpeechEvents()
        rec = _Recorder()
        events.on_user_speech_started = rec.make("started")
        events.mark_call_started(ts_ms=1_000_000)

        await events.fire_user_speech_started(
            timestamp_ms=1_000_500, vad_confidence=0.85
        )

        assert len(rec.calls) == 1
        name, payload = rec.calls[0]
        assert name == "started"
        assert payload["timestamp_ms"] == 1_000_500
        assert payload["vad_confidence"] == 0.85
        assert payload["audio_offset_ms"] == 500
        # State transitioned to "speaking".
        assert events.conversation_state["user"] == "speaking"

    async def test_user_speech_ended_fires_on_vad_negative_edge(self) -> None:
        events = SpeechEvents()
        rec = _Recorder()
        events.on_user_speech_ended = rec.make("ended")
        events.mark_call_started(ts_ms=2_000_000)

        # Simulate a positive edge (state := speaking) followed by trailing
        # negative edge — the runner consumes them as raw VAD edges.
        await events.fire_user_speech_started(timestamp_ms=2_000_100)
        await events.fire_user_speech_ended(
            timestamp_ms=2_000_700, speech_duration_ms=600
        )

        assert len(rec.calls) == 1
        name, payload = rec.calls[0]
        assert name == "ended"
        assert payload["speech_duration_ms"] == 600
        assert payload["audio_offset_ms"] == 700
        # User transitions back to listening on raw trailing edge.
        assert events.conversation_state["user"] == "listening"

    async def test_user_speech_eos_fires_after_trailing_silence(self) -> None:
        events = SpeechEvents()
        rec = _Recorder()
        events.on_user_speech_eos = rec.make("eos")

        await events.fire_user_speech_eos(
            trigger="vad_silence",
            trailing_silence_ms=420,
            transcript_so_far="I would like to book",
        )

        assert len(rec.calls) == 1
        name, payload = rec.calls[0]
        assert name == "eos"
        assert payload["trigger"] == "vad_silence"
        assert payload["trailing_silence_ms"] == 420
        assert payload["transcript_so_far"] == "I would like to book"
        # EOU advances turn_idx and transitions agent to "thinking".
        assert events.turn_idx == 1
        assert events.conversation_state["agent"] == "thinking"

    async def test_user_speech_eos_fires_after_semantic_turn_detector_agreement(
        self,
    ) -> None:
        events = SpeechEvents()
        rec = _Recorder()
        events.on_user_speech_eos = rec.make("eos")

        await events.fire_user_speech_eos(trigger="semantic_turn_detector")

        assert len(rec.calls) == 1
        _, payload = rec.calls[0]
        assert payload["trigger"] == "semantic_turn_detector"
        # Optional fields omitted when not provided.
        assert "trailing_silence_ms" not in payload
        assert "transcript_so_far" not in payload


@pytest.mark.unit
class TestAgentSpeechEdges:
    async def test_agent_speech_started_fires_on_first_wire_chunk(self) -> None:
        events = SpeechEvents()
        rec = _Recorder()
        events.on_agent_speech_started = rec.make("agent_started")

        # A user EOU brings the turn counter to 1 before the agent starts.
        await events.fire_user_speech_eos(trigger="vad_silence")
        await events.fire_agent_speech_started(
            tts_provider="elevenlabs", engine="openai_realtime"
        )

        assert len(rec.calls) == 1
        _, payload = rec.calls[0]
        assert payload["turn_idx"] == 1
        assert payload["tts_provider"] == "elevenlabs"
        assert payload["engine"] == "openai_realtime"
        assert events.conversation_state["agent"] == "speaking"

    async def test_agent_speech_ended_marks_interrupted_when_barge_in(self) -> None:
        events = SpeechEvents()
        rec = _Recorder()
        events.on_agent_speech_ended = rec.make("agent_ended")

        await events.fire_user_speech_eos(trigger="vad_silence")
        await events.fire_agent_speech_started(tts_provider="elevenlabs")
        await events.fire_agent_speech_ended(speech_duration_ms=1500, interrupted=True)

        assert len(rec.calls) == 1
        _, payload = rec.calls[0]
        assert payload["speech_duration_ms"] == 1500
        assert payload["interrupted"] is True
        assert payload["turn_idx"] == 1
        assert events.conversation_state["agent"] == "idle"


@pytest.mark.unit
class TestLLMAndTTSEvents:
    async def test_llm_first_token_fires_once_per_turn(self) -> None:
        events = SpeechEvents()
        rec = _Recorder()
        events.on_llm_token = rec.make("llm")

        await events.fire_user_speech_eos(trigger="vad_silence")  # turn 1
        await events.fire_llm_first_token(llm_provider="anthropic", model="claude")
        # Subsequent tokens within the same turn must NOT fire.
        await events.fire_llm_first_token(llm_provider="anthropic", model="claude")
        await events.fire_llm_first_token(llm_provider="anthropic", model="claude")

        assert len(rec.calls) == 1
        _, payload = rec.calls[0]
        assert payload["llm_provider"] == "anthropic"
        assert payload["model"] == "claude"
        assert payload["turn_idx"] == 1

        # New EOU re-arms first-token for the next turn.
        await events.fire_user_speech_eos(trigger="vad_silence")  # turn 2
        await events.fire_llm_first_token(llm_provider="openai", model="gpt-4o")
        assert len(rec.calls) == 2
        assert rec.calls[1][1]["turn_idx"] == 2

    async def test_audio_out_fires_once_per_turn(self) -> None:
        events = SpeechEvents()
        rec = _Recorder()
        events.on_audio_out = rec.make("audio")

        await events.fire_user_speech_eos(trigger="vad_silence")  # turn 1
        await events.fire_audio_out(tts_provider="elevenlabs")
        await events.fire_audio_out(tts_provider="elevenlabs")  # ignored

        assert len(rec.calls) == 1
        _, payload = rec.calls[0]
        assert payload["tts_provider"] == "elevenlabs"
        assert payload["turn_idx"] == 1


@pytest.mark.unit
class TestRobustness:
    async def test_callback_exception_does_not_propagate(self) -> None:
        events = SpeechEvents()

        async def boom(_payload: dict[str, Any]) -> None:
            raise RuntimeError("observer crashed")

        events.on_user_speech_started = boom
        # A misbehaving observer must not crash the live phone call. Any
        # exception is swallowed and logged.
        await events.fire_user_speech_started()

    async def test_chained_callback_runs_after_user_handler(self) -> None:
        """A user handler attached to the slot runs end-to-end. The runner's
        ``instrumentation/turn_taking.py`` wraps the slot so its observer
        composes on top — equivalent to writing a wrapper that calls the
        prior callback then its own. Verify the wrapper pattern works.
        """
        events = SpeechEvents()
        order: list[str] = []

        async def user_handler(_payload: dict[str, Any]) -> None:
            order.append("user")

        # Simulate the wrapper behaviour the runner installs at .install():
        prior = user_handler

        async def composed(payload: dict[str, Any]) -> None:
            await prior(payload)
            order.append("instrumentation")

        events.on_user_speech_started = composed
        await events.fire_user_speech_started()

        assert order == ["user", "instrumentation"]

    async def test_no_callbacks_set_yields_no_overhead(self) -> None:
        events = SpeechEvents()
        # No assertions on side effects — the test passes if no exception is
        # raised and dispatch is a near-no-op.
        await events.fire_user_speech_started()
        await events.fire_user_speech_ended(speech_duration_ms=120)
        await events.fire_user_speech_eos(trigger="vad_silence")
        await events.fire_agent_speech_started()
        await events.fire_agent_speech_ended(speech_duration_ms=800)
        await events.fire_llm_first_token(llm_provider="x", model="y")
        await events.fire_audio_out(tts_provider="x")

    async def test_otel_span_events_attached_to_call_span(self) -> None:
        """If the OTel API is importable and a span is recording, the
        dispatcher attaches each event with the documented name + attrs.
        We patch ``opentelemetry.trace.get_current_span`` to a stub that
        records calls so the test runs without a live exporter.
        """
        events = SpeechEvents()
        recorded: list[tuple[str, dict[str, Any]]] = []

        class _FakeSpan:
            def is_recording(self) -> bool:
                return True

            def add_event(
                self, name: str, attributes: dict[str, Any] | None = None
            ) -> None:
                recorded.append((name, attributes or {}))

        try:
            import opentelemetry.trace as otel_trace  # type: ignore[import-not-found]
        except ImportError:
            pytest.skip("opentelemetry not installed")

        with patch.object(otel_trace, "get_current_span", return_value=_FakeSpan()):
            await events.fire_user_speech_started()
            await events.fire_user_speech_eos(trigger="vad_silence")
            await events.fire_agent_speech_started(
                tts_provider="elevenlabs", engine="openai_realtime"
            )
            await events.fire_llm_first_token(
                llm_provider="anthropic", model="claude-haiku"
            )

        names = [n for n, _ in recorded]
        assert "patter.event.user_speech_started" in names
        assert "patter.event.user_speech_eos" in names
        assert "patter.event.agent_speech_started" in names
        assert "patter.event.llm_first_token" in names
        # The LLM event MUST carry the OTel GenAI semconv attributes so
        # backends parse it without a Patter-specific shim.
        llm_attrs = next(
            attrs for n, attrs in recorded if n == "patter.event.llm_first_token"
        )
        assert llm_attrs["gen_ai.request.model"] == "claude-haiku"
        assert llm_attrs["gen_ai.provider.name"] == "anthropic"


@pytest.mark.unit
class TestPatterIntegration:
    """The Patter class proxies the seven callbacks to ``speech_events``.
    Ensure both surfaces stay in sync (mutating one is visible on the other)
    and that ``conversation_state`` is read-only at the Patter level.
    """

    def _make_patter(self) -> Patter:
        # Local-mode Patter; carrier=None is allowed when we never call
        # serve(). The dispatcher is initialised at construction.
        return Patter()

    def test_callback_proxy_mirrors_speech_events(self) -> None:
        phone = self._make_patter()

        async def cb(_p: dict[str, Any]) -> None:
            return None

        phone.on_user_speech_started = cb
        assert phone.speech_events.on_user_speech_started is cb
        # And vice-versa.
        phone.speech_events.on_user_speech_ended = cb
        assert phone.on_user_speech_ended is cb

    def test_conversation_state_default(self) -> None:
        phone = self._make_patter()
        assert phone.conversation_state == {
            "user": "listening",
            "agent": "initializing",
        }

    async def test_conversation_state_reflects_dispatch(self) -> None:
        phone = self._make_patter()
        await phone.speech_events.fire_user_speech_started()
        assert phone.conversation_state["user"] == "speaking"
        await phone.speech_events.fire_user_speech_eos(trigger="vad_silence")
        # EOU commit transitions agent to thinking.
        assert phone.conversation_state["agent"] == "thinking"


@pytest.mark.unit
class TestStreamHandlerWiring:
    """Smoke-level test that the realtime stream-handler surfaces speech
    events when configured with a dispatcher. Keeps the assertion small
    (event names fired, in order) — the dispatcher payload schema is
    covered in the dispatcher tests above.
    """

    async def test_realtime_handler_emits_user_eos_on_transcript(self) -> None:
        from getpatter._speech_events import SpeechEvents
        from getpatter.stream_handler import OpenAIRealtimeStreamHandler

        # Build a minimally-configured handler. We bypass `start()` and call
        # ``_forward_events`` indirectly by feeding events through a fake
        # adapter — instead, we verify the helper methods on the base class
        # call into the dispatcher correctly. Full integration with real
        # adapters is covered by existing handler tests.
        events = SpeechEvents()
        recorder = _Recorder()
        events.on_user_speech_eos = recorder.make("eos")
        events.on_user_speech_ended = recorder.make("ended")
        events.on_agent_speech_started = recorder.make("agent_started")

        # Fabricate a handler instance without running the full constructor;
        # we only care about the helpers added by this PR.
        handler = OpenAIRealtimeStreamHandler.__new__(OpenAIRealtimeStreamHandler)
        handler.speech_events = events
        handler._user_speech_start_ms = None
        handler._agent_turn_start_ms = None

        await handler._emit_user_speech_started()
        await asyncio.sleep(0.001)
        await handler._emit_user_speech_ended()
        await handler._emit_user_speech_eos(
            trigger="vad_silence", transcript_so_far="hi"
        )
        await handler._emit_agent_speech_started(engine="openai_realtime")

        names = [name for name, _ in recorder.calls]
        assert "ended" in names
        assert "eos" in names
        assert "agent_started" in names
