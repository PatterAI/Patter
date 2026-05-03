"""Public error taxonomy for the Patter SDK.

Every Patter exception carries a stable, machine-readable :class:`ErrorCode`
on its ``code`` attribute. Downstream code can branch on the code without
relying on class name strings or message parsing.

The class hierarchy is preserved for backward compatibility — existing
``except PatterConnectionError`` paths keep working — and the enum is
purely additive.

Example::

    from getpatter import ErrorCode, PatterConfigError

    try:
        ...
    except PatterError as exc:
        if exc.code is ErrorCode.CONFIG:
            ...
"""

from __future__ import annotations

from enum import StrEnum
from typing import Optional


class ErrorCode(StrEnum):
    """Stable, machine-readable error codes attached to every Patter exception.

    Values are short, ``UPPER_SNAKE_CASE`` strings. Existing values must never
    change — downstream callers branch on them. New codes are additive.

    Mirrored byte-for-byte by the TypeScript ``ErrorCode`` const-object in
    ``libraries/typescript/src/errors.ts`` (see :doc:`sdk-parity`).
    """

    CONFIG = "CONFIG"
    """Invalid constructor args, missing required env var, frozen-config violation."""

    CONNECTION = "CONNECTION"
    """WebSocket connect failure, HTTP 5xx from provider, network error."""

    AUTH = "AUTH"
    """Provider rejected our credentials (HTTP 401/403, invalid signature)."""

    TIMEOUT = "TIMEOUT"
    """Provider response, voicemail post, or other awaited operation timed out."""

    RATE_LIMIT = "RATE_LIMIT"
    """Provider returned HTTP 429."""

    WEBHOOK_VERIFICATION = "WEBHOOK_VERIFICATION"
    """Twilio / Telnyx webhook signature verification failed."""

    INPUT_VALIDATION = "INPUT_VALIDATION"
    """Caller passed a malformed phone number, tool arg, etc."""

    PROVIDER_ERROR = "PROVIDER_ERROR"
    """Generic catch-all for unexpected upstream provider failures."""

    PROVISION = "PROVISION"
    """Phone number provisioning, webhook configuration, or carrier setup failed."""

    INTERNAL = "INTERNAL"
    """Assertion failed / unexpected internal state. Likely a Patter bug."""


class PatterError(Exception):
    """Base class for all errors raised by the Patter SDK.

    Subclasses set a class-level ``code`` default that names the matching
    :class:`ErrorCode` value. Callers can override per-instance by passing
    ``code=`` explicitly (rare).
    """

    #: Default :class:`ErrorCode` for this exception class. Subclasses override.
    code: ErrorCode = ErrorCode.INTERNAL

    def __init__(
        self,
        message: str = "",
        *,
        code: Optional[ErrorCode] = None,
    ) -> None:
        super().__init__(message)
        # Per-instance override; otherwise inherit the class-level default.
        if code is not None:
            self.code = code


class PatterConnectionError(PatterError):
    """Raised when the SDK cannot establish or maintain a network connection
    to a Patter backend or upstream provider."""

    code: ErrorCode = ErrorCode.CONNECTION


class AuthenticationError(PatterError):
    """Raised when API key or credential validation fails (HTTP 401/403 or
    invalid signature)."""

    code: ErrorCode = ErrorCode.AUTH


class ProvisionError(PatterError):
    """Raised when phone number provisioning, webhook configuration, or
    carrier setup fails."""

    code: ErrorCode = ErrorCode.PROVISION


class RateLimitError(PatterConnectionError):
    """Raised when a provider returns HTTP 429 on connect/upgrade."""

    code: ErrorCode = ErrorCode.RATE_LIMIT


__all__ = [
    "ErrorCode",
    "PatterError",
    "PatterConnectionError",
    "AuthenticationError",
    "ProvisionError",
    "RateLimitError",
]
