"""Tests for CallOrchestrator — transcript routing, barge-in, lifecycle events."""

import json
from unittest.mock import AsyncMock, MagicMock

from getpatter.providers.base import Transcript
from getpatter.services.call_orchestrator import CallOrchestrator
from getpatter.services.session_manager import CallSession


def _make_session(**overrides) -> CallSession:
    """Build a CallSession with sensible defaults and optional overrides."""
    defaults = dict(
        call_id="call_001",
        phone_number="+15551234567",
        direction="inbound",
        caller="+15559990000",
        callee="+15551234567",
        stt=None,
        tts=None,
        sdk_websocket=None,
        telephony_websocket=None,
        metadata={},
    )
    defaults.update(overrides)
    return CallSession(**defaults)


# --- Initialization ---


def test_init_stores_session_and_callbacks():
    session = _make_session()
    on_transcript = AsyncMock()
    on_start = AsyncMock()
    on_end = AsyncMock()

    orch = CallOrchestrator(
        session=session,
        needs_transcoding=True,
        on_transcript=on_transcript,
        on_call_start=on_start,
        on_call_end=on_end,
    )

    assert orch._session is session
    assert orch._needs_transcoding is True
    assert orch._is_speaking is False
    assert orch._on_transcript is on_transcript
    assert orch._on_call_start is on_start
    assert orch._on_call_end is on_end


def test_init_defaults():
    session = _make_session()
    orch = CallOrchestrator(session=session)

    assert orch._needs_transcoding is False
    assert orch._on_transcript is None
    assert orch._on_call_start is None
    assert orch._on_call_end is None


# --- handle_transcript ---


async def test_handle_transcript_final_invokes_callback():
    on_transcript = AsyncMock()
    session = _make_session()
    orch = CallOrchestrator(session=session, on_transcript=on_transcript)

    transcript = Transcript(text="hello world", is_final=True, confidence=0.95)
    await orch.handle_transcript(transcript)

    on_transcript.assert_awaited_once()
    payload = on_transcript.call_args[0][0]
    assert payload["text"] == "hello world"
    assert payload["call_id"] == "call_001"
    assert payload["caller"] == "+15559990000"
    assert payload["is_final"] is True


async def test_handle_transcript_final_no_callback():
    """Final transcript with no callback should not raise."""
    session = _make_session()
    orch = CallOrchestrator(session=session)

    transcript = Transcript(text="hello", is_final=True)
    await orch.handle_transcript(transcript)  # should not raise


async def test_handle_transcript_interim_no_speech_does_nothing():
    on_transcript = AsyncMock()
    session = _make_session()
    orch = CallOrchestrator(session=session, on_transcript=on_transcript)

    transcript = Transcript(text="partial", is_final=False)
    await orch.handle_transcript(transcript)

    on_transcript.assert_not_awaited()


async def test_handle_transcript_interim_during_speech_triggers_barge_in():
    """Interim transcript while speaking should trigger barge-in."""
    ws = AsyncMock()
    session = _make_session(telephony_websocket=ws)
    orch = CallOrchestrator(session=session, needs_transcoding=False)
    orch._is_speaking = True

    transcript = Transcript(text="interrupt", is_final=False)
    await orch.handle_transcript(transcript)

    # Barge-in should have cleared is_speaking
    assert orch._is_speaking is False
    ws.send_text.assert_awaited_once()
    sent = json.loads(ws.send_text.call_args[0][0])
    assert sent["event"] == "clear"


async def test_handle_transcript_interim_during_speech_empty_text_no_barge_in():
    """Interim transcript with empty text should NOT trigger barge-in."""
    ws = AsyncMock()
    session = _make_session(telephony_websocket=ws)
    orch = CallOrchestrator(session=session)
    orch._is_speaking = True

    transcript = Transcript(text="", is_final=False)
    await orch.handle_transcript(transcript)

    assert orch._is_speaking is True
    ws.send_text.assert_not_awaited()


# --- handle_barge_in ---


async def test_barge_in_clears_speaking_flag():
    session = _make_session(telephony_websocket=AsyncMock())
    orch = CallOrchestrator(session=session)
    orch._is_speaking = True

    await orch.handle_barge_in()

    assert orch._is_speaking is False


async def test_barge_in_sends_clear_event_no_transcoding():
    ws = AsyncMock()
    session = _make_session(telephony_websocket=ws)
    orch = CallOrchestrator(session=session, needs_transcoding=False)

    await orch.handle_barge_in()

    ws.send_text.assert_awaited_once()
    sent = json.loads(ws.send_text.call_args[0][0])
    assert sent["event"] == "clear"
    assert "streamSid" not in sent


async def test_barge_in_sends_clear_with_stream_sid_when_transcoding():
    ws = AsyncMock()
    session = _make_session(
        telephony_websocket=ws,
        metadata={"stream_sid": "MZ123"},
    )
    orch = CallOrchestrator(session=session, needs_transcoding=True)

    await orch.handle_barge_in()

    ws.send_text.assert_awaited_once()
    sent = json.loads(ws.send_text.call_args[0][0])
    assert sent["event"] == "clear"
    assert sent["streamSid"] == "MZ123"


async def test_barge_in_no_websocket_does_not_raise():
    session = _make_session(telephony_websocket=None)
    orch = CallOrchestrator(session=session)
    orch._is_speaking = True

    await orch.handle_barge_in()

    assert orch._is_speaking is False


# --- send_call_start / send_call_end ---


async def test_send_call_start_invokes_callback():
    on_start = AsyncMock()
    session = _make_session(direction="inbound")
    orch = CallOrchestrator(session=session, on_call_start=on_start)

    await orch.send_call_start()

    on_start.assert_awaited_once()
    payload = on_start.call_args[0][0]
    assert payload["call_id"] == "call_001"
    assert payload["caller"] == "+15559990000"
    assert payload["callee"] == "+15551234567"
    assert payload["direction"] == "inbound"


async def test_send_call_start_no_callback():
    session = _make_session()
    orch = CallOrchestrator(session=session)
    await orch.send_call_start()  # should not raise


async def test_send_call_end_invokes_callback():
    on_end = AsyncMock()
    session = _make_session()
    orch = CallOrchestrator(session=session, on_call_end=on_end)

    await orch.send_call_end()

    on_end.assert_awaited_once()
    payload = on_end.call_args[0][0]
    assert payload["call_id"] == "call_001"


async def test_send_call_end_no_callback():
    session = _make_session()
    orch = CallOrchestrator(session=session)
    await orch.send_call_end()  # should not raise
