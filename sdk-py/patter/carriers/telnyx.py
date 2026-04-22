"""Telnyx carrier credentials for Patter."""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = ["Carrier"]


@dataclass(frozen=True)
class Carrier:
    """Telnyx carrier credentials.

    Phase 2 dispatch narrows on :attr:`kind` to instantiate the underlying
    :class:`patter.providers.telnyx_adapter.TelnyxAdapter` on demand.

    Example::

        from patter.carriers import telnyx

        carrier = telnyx.Carrier()                           # reads env
        carrier = telnyx.Carrier(api_key="...", connection_id="...")
    """

    api_key: str = ""
    connection_id: str = ""
    public_key: str = ""

    def __post_init__(self) -> None:
        key = self.api_key or os.environ.get("TELNYX_API_KEY", "")
        conn = self.connection_id or os.environ.get("TELNYX_CONNECTION_ID", "")
        pub = self.public_key or os.environ.get("TELNYX_PUBLIC_KEY", "")
        if not key or not conn:
            raise ValueError(
                "Telnyx carrier requires api_key and connection_id. Pass them "
                "explicitly or set TELNYX_API_KEY and TELNYX_CONNECTION_ID in "
                "the environment."
            )
        object.__setattr__(self, "api_key", key)
        object.__setattr__(self, "connection_id", conn)
        object.__setattr__(self, "public_key", pub)

    @property
    def kind(self) -> str:
        """Stable discriminator used for Phase 2 dispatch."""
        return "telnyx"
