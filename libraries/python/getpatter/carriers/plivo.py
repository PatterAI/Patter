"""Plivo carrier credentials for Patter."""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = ["Carrier"]


@dataclass(frozen=True)
class Carrier:
    """Plivo carrier credentials.

    Phase 2 dispatch narrows on :attr:`kind` to instantiate the underlying
    :class:`getpatter.providers.plivo_adapter.PlivoAdapter` on demand.

    Plivo authenticates REST calls (outbound dial, hangup, recording) with
    HTTP Basic ``auth_id:auth_token`` and verifies inbound webhooks with the
    V3 signature scheme (HMAC-SHA256 keyed on ``auth_token``) — so unlike
    Telnyx there is no separate asymmetric ``public_key`` to carry.

    Example::

        from getpatter.carriers import plivo

        carrier = plivo.Carrier()                          # reads env
        carrier = plivo.Carrier(auth_id="MA...", auth_token="...")
    """

    auth_id: str = ""
    auth_token: str = ""

    def __post_init__(self) -> None:
        auth_id = self.auth_id or os.environ.get("PLIVO_AUTH_ID", "")
        auth_token = self.auth_token or os.environ.get("PLIVO_AUTH_TOKEN", "")
        if not auth_id or not auth_token:
            raise ValueError(
                "Plivo carrier requires auth_id and auth_token. Pass them "
                "explicitly or set PLIVO_AUTH_ID and PLIVO_AUTH_TOKEN in "
                "the environment."
            )
        # Frozen dataclass: use object.__setattr__ to backfill resolved values.
        object.__setattr__(self, "auth_id", auth_id)
        object.__setattr__(self, "auth_token", auth_token)

    @property
    def kind(self) -> str:
        """Stable discriminator used for Phase 2 dispatch."""
        return "plivo"
