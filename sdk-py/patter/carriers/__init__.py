"""Telephony carrier credential holders for Patter.

Each submodule exposes a ``Carrier`` frozen dataclass carrying the minimal
credentials needed to dispatch to the matching provider in Phase 2. The
``.kind`` property returns a stable string (``"twilio"`` / ``"telnyx"``)
for runtime narrowing.

Usage::

    from patter.carriers import twilio
    carrier = twilio.Carrier()              # reads TWILIO_* env vars
"""

from __future__ import annotations

__all__ = ["twilio", "telnyx"]
