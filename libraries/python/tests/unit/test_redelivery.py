"""Unit tests for opt-in barge-in RE-DELIVERY.

When the agent is interrupted mid-answer and the un-heard remainder still
matters, ``Agent.redeliver_interrupted`` arms a one-shot system nudge on the
next turn so the LLM answers the caller's new message first and then resumes
the unfinished idea naturally — instead of silently dropping what the caller
never heard.

The capture happens in ``_do_cancel_for_barge_in`` (right after the completed-
turn truncation, before the playback cursor is reset) and the injection
happens on the next turn via ``_arm_pending_redelivery_nudge`` +
``LLMLoop.set_redelivery_nudge`` / ``_build_messages``.
"""

from __future__ import annotations

import time
from collections import deque
from unittest.mock import AsyncMock

import pytest

from getpatter.services.llm_loop import LLMLoop
from getpatter.services.redelivery import (
    DEFAULT_MIN_UNSAID_WORDS,
    MinUnsaidWordsPolicy,
    RedeliveryPolicy,
    build_redelivery_nudge,
)
from getpatter.stream_handler import PipelineStreamHandler

from tests.conftest import make_agent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_audio_sender() -> AsyncMock:
    sender = AsyncMock()
    sender._input_is_mulaw_8k = False
    return sender


def _make_loop(system_prompt: str = "You are a test assistant.") -> LLMLoop:
    """A minimal real LLMLoop shell for ``_build_messages`` / setter tests."""
    loop = LLMLoop.__new__(LLMLoop)
    loop._system_prompt = system_prompt
    loop._context_summary = ""
    loop._redelivery_nudge = ""
    return loop


def _make_handler(
    *,
    redeliver_interrupted: bool = False,
    redelivery_policy: RedeliveryPolicy | None = None,
    with_loop: bool = True,
) -> PipelineStreamHandler:
    handler = PipelineStreamHandler(
        agent=make_agent(
            redeliver_interrupted=redeliver_interrupted,
            redelivery_policy=redelivery_policy,
        ),
        audio_sender=_make_audio_sender(),
        call_id="call-redeliver",
        caller="+15551110000",
        callee="+15552220000",
        resolved_prompt="p",
        metrics=None,
        for_twilio=True,
        on_transcript=None,
        conversation_history=deque(maxlen=10),
        transcript_entries=deque(maxlen=10),
    )
    handler.on_message = None
    handler._llm_loop = _make_loop() if with_loop else None
    return handler


def _setup_interrupted_turn(
    handler: PipelineStreamHandler,
    segments: list[tuple[str, float, float]],
    total_s: float,
    remaining_s: float,
) -> None:
    """Arrange a turn that has played ``total_s - remaining_s`` seconds."""
    handler._is_speaking = True
    handler._turn_spoken_segments = list(segments)
    handler._turn_playback_total_s = total_s
    handler._playback_buffered_until = time.time() + remaining_s


# A three-sentence reply, half heard: heard = "Sentence one here. The second
# sentence"; unsaid = "goes here. Third and final sentence." (5 words).
_HALF_HEARD = [
    ("Sentence one here.", 0.0, 2.0),
    ("The second sentence goes here.", 2.0, 2.0),
    ("Third and final sentence.", 4.0, 0.0),
]


def _redelivery_messages(messages: list[dict]) -> list[dict]:
    return [
        m
        for m in messages
        if m["role"] == "system"
        and "You were interrupted while speaking." in m["content"]
    ]


# ---------------------------------------------------------------------------
# Pure policy + nudge builder
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRedeliveryPolicy:
    def test_default_min_words_constant(self) -> None:
        assert DEFAULT_MIN_UNSAID_WORDS >= 2

    def test_min_words_policy_arms_on_substantial_unsaid(self) -> None:
        policy = MinUnsaidWordsPolicy()
        assert policy.should_redeliver(
            heard="a b", unsaid="one two three four", heard_everything=False
        )

    def test_min_words_policy_declines_trivial_unsaid(self) -> None:
        policy = MinUnsaidWordsPolicy()
        assert not policy.should_redeliver(
            heard="a b", unsaid="one", heard_everything=False
        )

    def test_min_words_policy_declines_when_heard_everything(self) -> None:
        policy = MinUnsaidWordsPolicy()
        assert not policy.should_redeliver(
            heard="everything", unsaid="", heard_everything=True
        )

    def test_min_words_rejects_below_one(self) -> None:
        with pytest.raises(ValueError):
            MinUnsaidWordsPolicy(min_words=0)

    def test_is_runtime_checkable_protocol(self) -> None:
        assert isinstance(MinUnsaidWordsPolicy(), RedeliveryPolicy)

    def test_nudge_quotes_heard_and_unsaid(self) -> None:
        nudge = build_redelivery_nudge("the heard part", "the unsaid part")
        assert "Recently heard by the user: the heard part." in nudge
        assert "Not yet heard by the user: the unsaid part." in nudge
        assert "Answer the user's latest message first." in nudge

    def test_nudge_phrases_empty_heard_as_nothing_yet(self) -> None:
        nudge = build_redelivery_nudge("", "the unsaid part")
        assert "had not yet heard any of it" in nudge
        assert "Recently heard by the user:" not in nudge


# ---------------------------------------------------------------------------
# LLMLoop one-shot injection
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLoopRedeliveryNudge:
    def test_default_off_no_extra_system_message(self) -> None:
        # (a) with nothing armed, no re-delivery message is ever built.
        loop = _make_loop()
        messages = loop._build_messages([], "hi")
        assert _redelivery_messages(messages) == []
        assert messages[0]["content"] == "You are a test assistant."

    def test_nudge_injected_once_then_consumed(self) -> None:
        # (b) armed nudge appears exactly once and is GONE on the next build.
        loop = _make_loop()
        loop.set_redelivery_nudge(build_redelivery_nudge("heard bit", "unsaid bit"))

        first = loop._build_messages([], "new question")
        nudges = _redelivery_messages(first)
        assert len(nudges) == 1
        assert "Recently heard by the user: heard bit." in nudges[0]["content"]
        assert "Not yet heard by the user: unsaid bit." in nudges[0]["content"]

        second = loop._build_messages([], "another question")
        assert _redelivery_messages(second) == []

    def test_nudge_is_separate_message_and_coexists_with_summary(self) -> None:
        # (f) the nudge is its own system message (NOT concatenated into the
        # main system prompt) and coexists with a compaction summary.
        loop = _make_loop("MAIN SYSTEM PROMPT")
        loop.set_context_summary("older turns condensed here")
        loop.set_redelivery_nudge(build_redelivery_nudge("h", "u u u"))

        messages = loop._build_messages([], "q")
        system_msgs = [m for m in messages if m["role"] == "system"]
        assert len(system_msgs) == 3  # main + summary + redelivery
        assert messages[0]["content"] == "MAIN SYSTEM PROMPT"
        assert "You were interrupted" not in messages[0]["content"]
        summary_msgs = [m for m in system_msgs if "condensed" in m["content"]]
        nudge_msgs = _redelivery_messages(messages)
        assert len(summary_msgs) == 1
        assert len(nudge_msgs) == 1

    def test_setter_clears_with_empty_string(self) -> None:
        loop = _make_loop()
        loop.set_redelivery_nudge("something")
        loop.set_redelivery_nudge("")
        assert _redelivery_messages(loop._build_messages([], "q")) == []


# ---------------------------------------------------------------------------
# Handler capture at barge-in + injection on the next turn
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestHandlerRedeliveryCapture:
    async def test_default_off_never_arms(self) -> None:
        # (a) redeliver_interrupted defaults False ⇒ nothing armed, no nudge.
        handler = _make_handler(redeliver_interrupted=False)
        _setup_interrupted_turn(handler, _HALF_HEARD, total_s=6.0, remaining_s=3.0)

        await handler._do_cancel_for_barge_in("wait what about billing")

        assert handler._pending_redelivery is None
        handler._arm_pending_redelivery_nudge()
        messages = handler._llm_loop._build_messages([], "new question")
        assert _redelivery_messages(messages) == []

    async def test_on_arms_and_injects_next_turn_then_one_shot(self) -> None:
        # (b) ON + substantial unsaid ⇒ armed, injected once, gone after.
        handler = _make_handler(redeliver_interrupted=True)
        _setup_interrupted_turn(handler, _HALF_HEARD, total_s=6.0, remaining_s=3.0)

        await handler._do_cancel_for_barge_in("wait what about billing")

        assert handler._pending_redelivery is not None
        heard, unsaid = handler._pending_redelivery
        assert heard == "Sentence one here. The second sentence"
        assert unsaid == "goes here. Third and final sentence."

        # Dispatch simulation: arm the nudge for this turn, then build.
        handler._arm_pending_redelivery_nudge()
        assert handler._pending_redelivery is None  # disarmed

        first = handler._llm_loop._build_messages([], "what about billing?")
        nudges = _redelivery_messages(first)
        assert len(nudges) == 1
        assert (
            "Not yet heard by the user: goes here. Third and final sentence."
            in nudges[0]["content"]
        )

        # One-shot: the turn after, no nudge (nothing re-armed).
        handler._arm_pending_redelivery_nudge()  # nothing pending → no-op
        second = handler._llm_loop._build_messages([], "and shipping?")
        assert _redelivery_messages(second) == []

    async def test_on_but_heard_everything_no_arm(self) -> None:
        # (c) ON + heard everything ⇒ nothing to resume ⇒ no arm.
        handler = _make_handler(redeliver_interrupted=True)
        # remaining_s = 0 ⇒ played position == total ⇒ everything heard.
        _setup_interrupted_turn(handler, _HALF_HEARD, total_s=6.0, remaining_s=0.0)

        await handler._do_cancel_for_barge_in("ok thanks")

        assert handler._pending_redelivery is None

    async def test_on_but_trivial_unsaid_below_threshold_no_arm(self) -> None:
        # (d) ON + only a trailing word or two un-heard ⇒ below default gate.
        handler = _make_handler(redeliver_interrupted=True)
        segments = [
            ("Alpha bravo charlie delta echo foxtrot", 0.0, 5.0),
            ("golf hotel", 5.0, 1.0),
        ]
        # remaining 0.4 s ⇒ heard ~5.6 s ⇒ only the tail word(s) un-heard.
        _setup_interrupted_turn(handler, segments, total_s=6.0, remaining_s=0.4)

        # Sanity: the un-heard remainder really is trivial (< threshold).
        split = handler._heard_response_split()
        assert split is not None
        _h, unsaid, heard_everything = split
        assert not heard_everything
        assert 0 < len(unsaid.split()) < DEFAULT_MIN_UNSAID_WORDS

        await handler._do_cancel_for_barge_in("golf hotel")

        assert handler._pending_redelivery is None

    async def test_custom_policy_overrides_default_gate(self) -> None:
        # (e) a custom RedeliveryPolicy can arm where the default declines
        # (here: a trivial tail the default MinUnsaidWordsPolicy would skip).
        class AlwaysRedeliver:
            def should_redeliver(
                self, *, heard: str, unsaid: str, heard_everything: bool
            ) -> bool:
                return bool(unsaid)

        handler = _make_handler(
            redeliver_interrupted=True, redelivery_policy=AlwaysRedeliver()
        )
        segments = [
            ("Alpha bravo charlie delta echo foxtrot", 0.0, 5.0),
            ("golf hotel", 5.0, 1.0),
        ]
        _setup_interrupted_turn(handler, segments, total_s=6.0, remaining_s=0.4)

        await handler._do_cancel_for_barge_in("golf hotel")

        # The default policy would have declined this trivial tail; the custom
        # one arms it.
        assert handler._pending_redelivery is not None

        # And a policy that never arms suppresses even a substantial remainder.
        class NeverRedeliver:
            def should_redeliver(self, **_kwargs: object) -> bool:
                return False

        handler2 = _make_handler(
            redeliver_interrupted=True, redelivery_policy=NeverRedeliver()
        )
        _setup_interrupted_turn(handler2, _HALF_HEARD, total_s=6.0, remaining_s=3.0)
        await handler2._do_cancel_for_barge_in("wait what about billing")
        assert handler2._pending_redelivery is None

    async def test_arm_nudge_no_loop_is_safe(self) -> None:
        # on_message path (no built-in loop): capture may arm, injection is a
        # no-op that still disarms so nothing leaks.
        handler = _make_handler(redeliver_interrupted=True, with_loop=False)
        _setup_interrupted_turn(handler, _HALF_HEARD, total_s=6.0, remaining_s=3.0)
        await handler._do_cancel_for_barge_in("wait what about billing")
        assert handler._pending_redelivery is not None
        handler._arm_pending_redelivery_nudge()
        assert handler._pending_redelivery is None
