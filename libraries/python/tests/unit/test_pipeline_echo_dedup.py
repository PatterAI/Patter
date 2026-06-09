"""Echo-guard, back-to-back dedup, and interrupted-turn marking for the
pipeline turn-taking path — the residual Hermes/OpenClaw barge-in fixes.

Root causes (live Hermes test, PATTER_FORWARD_STT_WHILE_SPEAKING=1, no AEC):
* the agent's own TTS bled into Deepgram and was transcribed as a phantom
  ("che tu l'hai"), firing a false barge-in (legacy "any transcript = cancel");
* the real follow-up final arriving <0.5s later was dropped by the back-to-back
  filter even though its text was completely different → empty [interrupted] turn;
* the interrupted assistant turn was stored ungrounded, poisoning the next turn.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from unittest.mock import AsyncMock, MagicMock

import pytest

from getpatter.providers.base import Transcript
from getpatter.stream_handler import (
    PipelineStreamHandler,
    _is_near_duplicate,
    _looks_like_echo,
    _normalize_for_echo,
)

from tests.conftest import make_agent


def _make_handler() -> PipelineStreamHandler:
    handler = PipelineStreamHandler(
        agent=make_agent(),
        audio_sender=AsyncMock(),
        call_id="call-echo",
        caller="+15551110000",
        callee="+15552220000",
        resolved_prompt="p",
        metrics=MagicMock(),
        for_twilio=True,
        on_transcript=None,
        conversation_history=deque(maxlen=20),
        transcript_entries=deque(maxlen=20),
    )
    handler.on_message = None
    handler._stt = AsyncMock()
    return handler


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEchoHelpers:
    def test_normalize_strips_punct_and_case(self) -> None:
        assert _normalize_for_echo("Ciao, come VA?!") == "ciao come va"

    def test_substring_fragment_is_echo(self) -> None:
        agent = "Certo, ti racconto una storia molto lunga sul mare aperto"
        # A long (>=4 word) verbatim fragment of the agent's speech is echo.
        assert _looks_like_echo("ti racconto una storia molto", agent) is True

    def test_high_word_overlap_is_echo(self) -> None:
        agent = "che tu lo voglia o no, te l'ho già detto chiaramente"
        # garbled >=4-word echo fragment whose words are mostly in the agent text
        assert _looks_like_echo("che tu lo voglia detto", agent) is True

    def test_short_answer_repeating_agent_is_not_echo(self) -> None:
        # The key false-positive guard: a 1-3 word caller answer that picks one
        # of the agent's offered words must NEVER be classified as echo.
        agent = "preferisci lunedì o martedì per l'appuntamento"
        assert _looks_like_echo("lunedì", agent) is False
        assert _looks_like_echo("monday at two", agent) is False
        assert _looks_like_echo("sì va bene", agent) is False

    def test_unrelated_user_speech_is_not_echo(self) -> None:
        agent = "Sto bene grazie, sono pronto ad aiutarti col tuo problema"
        assert _looks_like_echo("fermati dimmi solo interrotto", agent) is False

    def test_empty_inputs_not_echo(self) -> None:
        assert _looks_like_echo("", "qualcosa") is False
        assert _looks_like_echo("qualcosa", "") is False

    def test_near_duplicate_substring_and_exact(self) -> None:
        assert _is_near_duplicate("ciao come va", "ciao come va") is True
        assert _is_near_duplicate("ciao come", "ciao come va") is True  # prefix
        assert _is_near_duplicate("ciao come va bene", "ciao come va") is True
        assert _is_near_duplicate("fermati subito", "dimmi una storia") is False


# ---------------------------------------------------------------------------
# _commit_transcript
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCommitTranscriptEchoAndDedup:
    def test_echo_dropped_while_speaking_with_forward_flag(self) -> None:
        h = _make_handler()
        h._forward_stt_while_speaking = True
        h._is_speaking = True
        h._current_agent_spoken_text = "ti racconto una storia lunga sul mare"
        assert h._commit_transcript("ti racconto una storia lunga") is False

    def test_echo_not_dropped_when_flag_off(self) -> None:
        h = _make_handler()
        h._forward_stt_while_speaking = False  # default
        h._is_speaking = True
        h._current_agent_spoken_text = "ti racconto una storia lunga sul mare"
        # Flag off → echo guard inert → normal commit (real user could legitimately
        # echo words; we only filter under the forward-STT echo-prone config).
        assert h._commit_transcript("ti racconto una storia lunga") is True

    def test_echo_not_dropped_when_idle(self) -> None:
        h = _make_handler()
        h._forward_stt_while_speaking = True
        h._is_speaking = False  # post-turn user reply, not an echo window
        h._current_agent_spoken_text = "ti racconto una storia lunga sul mare"
        assert h._commit_transcript("ti racconto una storia lunga") is True

    def test_different_followup_within_500ms_not_dropped(self) -> None:
        h = _make_handler()
        h._last_commit_text = "dimmi una storia"
        h._last_commit_at = time.time()  # just now
        # A genuinely different utterance arriving <0.5s later must survive
        # (the empty-[interrupted]-turn fix).
        assert h._commit_transcript("fermati dimmi solo interrotto") is True

    def test_near_duplicate_within_500ms_dropped(self) -> None:
        h = _make_handler()
        h._last_commit_text = "fermati dimmi solo"
        h._last_commit_at = time.time()
        # Deepgram speech_final then is_final for the same utterance (a superset)
        # is still de-duplicated.
        assert h._commit_transcript("fermati dimmi solo interrotto") is False


# ---------------------------------------------------------------------------
# _handle_barge_in echo guard
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestHandleBargeInEchoGuard:
    async def test_echo_transcript_does_not_barge_in(self) -> None:
        h = _make_handler()
        h._forward_stt_while_speaking = True
        h._is_speaking = True
        h._tail_grace_active = False
        h._can_barge_in = lambda: True  # type: ignore[assignment]
        h._current_agent_spoken_text = "ti racconto una storia lunga sul mare aperto"

        await h._handle_barge_in(
            Transcript(text="ti racconto una storia lunga", is_final=True, confidence=0.9)
        )

        # No cancel: the agent's own echo must not interrupt it.
        h.audio_sender.send_clear.assert_not_awaited()
        assert h._is_speaking is True

    async def test_real_speech_still_barges_in(self) -> None:
        h = _make_handler()
        h._forward_stt_while_speaking = True
        h._is_speaking = True
        h._tail_grace_active = False
        h._can_barge_in = lambda: True  # type: ignore[assignment]
        h._current_agent_spoken_text = "ti racconto una storia lunga sul mare aperto"

        await h._handle_barge_in(
            Transcript(text="fermati dimmi solo interrotto", is_final=True, confidence=0.9)
        )

        h.audio_sender.send_clear.assert_awaited()
        assert h._is_speaking is False


# ---------------------------------------------------------------------------
# Interrupted-turn marking
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestInterruptedTurnMarking:
    async def test_interrupted_response_gets_marker(self) -> None:
        h = _make_handler()

        class _FakeTTS:
            output_format = "pcm_16000"

            async def synthesize(self, text: str):
                yield b"\x00\x00" * 80

        h._tts = _FakeTTS()  # type: ignore[assignment]

        async def _result():
            yield "Ti racconto. "
            # Simulate a barge-in cancelling the stream mid-turn.
            h._llm_cancel_event.set()
            yield "Questo non si sente."

        text = await h._process_streaming_response(_result(), "call-echo")

        assert h._last_response_interrupted is True
        assert text.endswith("[interrupted by caller]")
        assert "Ti racconto." in text

    async def test_complete_response_no_marker(self) -> None:
        h = _make_handler()

        class _FakeTTS:
            output_format = "pcm_16000"

            async def synthesize(self, text: str):
                yield b"\x00\x00" * 80

        h._tts = _FakeTTS()  # type: ignore[assignment]

        async def _result():
            yield "Tutto bene, grazie. "

        text = await h._process_streaming_response(_result(), "call-echo")

        assert h._last_response_interrupted is False
        assert "[interrupted by caller]" not in text
