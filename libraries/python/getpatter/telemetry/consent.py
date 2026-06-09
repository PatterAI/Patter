"""Resolve whether anonymous telemetry is enabled.

OPT-OUT model: telemetry is **on by default**, matching the open-source norm
(Next.js / Astro / Gatsby / .NET CLI). The env vars and the constructor flag are
*disable* switches, not opt-in switches.

Precedence (first match wins):

1. ``DO_NOT_TRACK`` truthy            -> OFF  (cross-tool kill switch, always wins)
2. ``PATTER_TELEMETRY_DISABLED``      -> OFF  (Patter-specific kill switch)
3. persisted opt-out marker present   -> OFF  (``getpatter telemetry disable``)
4. ``flag`` is ``False``              -> OFF  (explicit in-code opt-out)
5. CI / test runner detected          -> OFF
6. default                            -> ON
"""

from __future__ import annotations

import os

from getpatter.telemetry.env import is_ci, is_test, is_truthy

# Top-level import (matches consent.ts). ``install_id`` depends only on the stdlib,
# so there is no import cycle back to this module.
from getpatter.telemetry.install_id import is_opted_out


def is_enabled(flag: bool | None = None) -> bool:
    """Resolve telemetry enablement.

    ``flag`` is the value of the public ``Patter(telemetry=...)`` option:
    ``None`` means "not specified" (fall through to the default-ON behaviour),
    ``False`` is an explicit in-code opt-out, ``True`` is an explicit opt-in that
    still yields to ``DO_NOT_TRACK`` / the kill switch / CI detection above it.
    """
    if is_truthy(os.getenv("DO_NOT_TRACK")):
        return False
    if is_truthy(os.getenv("PATTER_TELEMETRY_DISABLED")):
        return False
    # Persisted, machine-level opt-out written by ``getpatter telemetry disable``.
    # Read-only — resolving consent never writes to the filesystem.
    if is_opted_out():
        return False
    if flag is False:
        return False
    if is_ci() or is_test():
        return False
    return True
