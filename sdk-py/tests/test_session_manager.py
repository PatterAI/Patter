"""Tests for SessionManager and CallSession."""

from unittest.mock import AsyncMock

from patter.services.session_manager import CallSession, SessionManager


# --- SessionManager ---


def test_create_session_returns_call_session():
    mgr = SessionManager()
    session = mgr.create_session(
        call_id="c1",
        phone_number="+15551111111",
        direction="inbound",
        caller="+15559990000",
        callee="+15551111111",
    )

    assert isinstance(session, CallSession)
    assert session.call_id == "c1"
    assert session.phone_number == "+15551111111"
    assert session.direction == "inbound"
    assert session.caller == "+15559990000"
    assert session.callee == "+15551111111"


def test_get_session_existing():
    mgr = SessionManager()
    created = mgr.create_session("c1", "+1", "inbound", "+2", "+1")

    found = mgr.get_session("c1")

    assert found is created


def test_get_session_unknown_returns_none():
    mgr = SessionManager()

    assert mgr.get_session("nonexistent") is None


def test_remove_session_cleans_up():
    mgr = SessionManager()
    mgr.create_session("c1", "+1", "inbound", "+2", "+1")

    mgr.remove_session("c1")

    assert mgr.get_session("c1") is None


def test_remove_session_unknown_does_not_raise():
    mgr = SessionManager()
    mgr.remove_session("ghost")  # should not raise


def test_find_by_number_matches():
    mgr = SessionManager()
    mgr.create_session("c1", "+15550001111", "inbound", "+a", "+b")
    mgr.create_session("c2", "+15550002222", "inbound", "+c", "+d")
    mgr.create_session("c3", "+15550001111", "outbound", "+e", "+f")

    results = mgr.find_by_number("+15550001111")

    assert len(results) == 2
    ids = {s.call_id for s in results}
    assert ids == {"c1", "c3"}


def test_find_by_number_no_match():
    mgr = SessionManager()
    mgr.create_session("c1", "+15550001111", "inbound", "+a", "+b")

    assert mgr.find_by_number("+19999999999") == []


# --- CallSession.cleanup ---


async def test_cleanup_closes_all_resources():
    stt = AsyncMock()
    tts = AsyncMock()
    tel_ws = AsyncMock()
    sdk_ws = AsyncMock()

    session = CallSession(
        call_id="c1",
        phone_number="+1",
        direction="inbound",
        caller="+2",
        callee="+1",
        stt=stt,
        tts=tts,
        telephony_websocket=tel_ws,
        sdk_websocket=sdk_ws,
    )

    await session.cleanup()

    stt.close.assert_awaited_once()
    tts.close.assert_awaited_once()
    tel_ws.close.assert_awaited_once()
    sdk_ws.close.assert_awaited_once()


async def test_cleanup_handles_websocket_close_error():
    """Cleanup should not raise even if websocket close() fails."""
    tel_ws = AsyncMock()
    tel_ws.close.side_effect = RuntimeError("connection lost")
    sdk_ws = AsyncMock()
    sdk_ws.close.side_effect = RuntimeError("connection lost")

    session = CallSession(
        call_id="c1",
        phone_number="+1",
        direction="inbound",
        caller="+2",
        callee="+1",
        telephony_websocket=tel_ws,
        sdk_websocket=sdk_ws,
    )

    await session.cleanup()  # should not raise


async def test_cleanup_with_no_resources():
    """Cleanup on a bare session (all None) should not raise."""
    session = CallSession(
        call_id="c1",
        phone_number="+1",
        direction="inbound",
        caller="+2",
        callee="+1",
    )

    await session.cleanup()  # should not raise
