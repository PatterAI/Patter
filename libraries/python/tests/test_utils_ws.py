"""Compat helper covers both modern (``state``) and legacy (``closed``)
``websockets`` client shapes. Regression for upstream issue #111."""

from __future__ import annotations

from getpatter.utils.ws import is_ws_alive


class _ModernOpenWS:
    """Mimics ``websockets>=12`` `ClientConnection` (state=1 means OPEN)."""

    state = 1


class _ModernClosedWS:
    state = 3  # CLOSED


class _ModernByCloseCodeOpen:
    """Some shapes only expose ``close_code`` (None == still open)."""

    close_code = None


class _ModernByCloseCodeClosed:
    close_code = 1000


class _LegacyOpenWS:
    """Mimics ``websockets<11`` API (``closed`` bool property)."""

    closed = False


class _LegacyClosedWS:
    closed = True


class _UnknownShape:
    """Neither state nor close_code nor closed — must fail closed."""


def test_modern_open():
    assert is_ws_alive(_ModernOpenWS()) is True


def test_modern_closed():
    assert is_ws_alive(_ModernClosedWS()) is False


def test_modern_open_via_close_code():
    assert is_ws_alive(_ModernByCloseCodeOpen()) is True


def test_modern_closed_via_close_code():
    assert is_ws_alive(_ModernByCloseCodeClosed()) is False


def test_legacy_open():
    assert is_ws_alive(_LegacyOpenWS()) is True


def test_legacy_closed():
    assert is_ws_alive(_LegacyClosedWS()) is False


def test_unknown_shape_defaults_closed():
    """Unknown WS shapes must NOT be reported alive — handing a dead socket
    to the live adapter is worse than re-opening."""
    assert is_ws_alive(_UnknownShape()) is False


def test_state_intenum_open():
    """``state`` may be an IntEnum where OPEN == 1."""
    from enum import IntEnum

    class _State(IntEnum):
        OPEN = 1
        CLOSED = 3

    class WS:
        state = _State.OPEN

    assert is_ws_alive(WS()) is True
