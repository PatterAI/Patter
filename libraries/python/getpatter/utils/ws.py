"""WebSocket compatibility utilities.

Patter pins ``websockets>=14,<16`` in ``pyproject.toml``. The modern
``websockets`` client (``websockets>=12``) no longer exposes a
``ws.closed`` property — it surfaces ``state`` (an ``IntEnum`` where
``OPEN == 1``) and ``close_code`` (``None`` while the socket is open).

This helper papers over both APIs so SDK code can ask *"is this
WebSocket still alive?"* without sprinkling version checks everywhere.
"""

from __future__ import annotations


def is_ws_alive(ws: object) -> bool:
    """Best-effort liveness check across ``websockets`` library versions.

    Returns ``True`` only when we can confirm the socket is OPEN. Never
    defaults to ``True`` on unknown shapes — handing a dead socket to a
    live adapter is worse than re-opening a fresh one.
    """
    state = getattr(ws, "state", None)
    if state is not None:
        try:
            return int(state) == 1
        except Exception:
            return getattr(state, "name", "").upper() == "OPEN"
    close_code = getattr(ws, "close_code", "__unset__")
    if close_code != "__unset__":
        return close_code is None
    closed = getattr(ws, "closed", None)
    if closed is None:
        return False
    return not bool(closed)
