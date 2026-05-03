"""Telephony adapters — Twilio + Telnyx webhook + media-stream bridges.

Internal submodule. The public API surface for telephony is exposed through
``getpatter.Twilio`` / ``getpatter.Telnyx`` (carriers credential holders) and
through the FastAPI app mounted by :class:`getpatter.client.Patter`.
"""

from __future__ import annotations

__all__ = ["common", "twilio", "telnyx"]
