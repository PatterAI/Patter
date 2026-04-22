"""Twilio carrier credentials for Patter."""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = ["Carrier"]


@dataclass(frozen=True)
class Carrier:
    """Twilio carrier credentials.

    Phase 2 dispatch narrows on :attr:`kind` to instantiate the underlying
    :class:`patter.providers.twilio_adapter.TwilioAdapter` on demand.

    Example::

        from patter.carriers import twilio

        carrier = twilio.Carrier()                           # reads env
        carrier = twilio.Carrier(account_sid="AC...", auth_token="...")
    """

    account_sid: str = ""
    auth_token: str = ""

    def __post_init__(self) -> None:
        sid = self.account_sid or os.environ.get("TWILIO_ACCOUNT_SID", "")
        token = self.auth_token or os.environ.get("TWILIO_AUTH_TOKEN", "")
        if not sid or not token:
            raise ValueError(
                "Twilio carrier requires account_sid and auth_token. Pass them "
                "explicitly or set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN in "
                "the environment."
            )
        # Frozen dataclass: use object.__setattr__ to backfill resolved values.
        object.__setattr__(self, "account_sid", sid)
        object.__setattr__(self, "auth_token", token)

    @property
    def kind(self) -> str:
        """Stable discriminator used for Phase 2 dispatch."""
        return "twilio"
