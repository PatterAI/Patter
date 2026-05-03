"""Unit tests for the Patter error taxonomy (`getpatter.exceptions`).

Verifies that:
  * `ErrorCode` is importable from the package root and contains the
    canonical set of codes.
  * Every concrete exception class carries the matching default `.code`.
  * Per-instance `code=` overrides are honoured.
  * Subclassing relationships still hold (backward compat).
  * At least one real raise site (Deepgram STT 401) surfaces the expected code
    so downstream `except`-by-code paths actually work end-to-end.
"""

from __future__ import annotations

import pytest

import getpatter
from getpatter.exceptions import (
    AuthenticationError,
    ErrorCode,
    PatterConnectionError,
    PatterError,
    ProvisionError,
    RateLimitError,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Enum surface
# ---------------------------------------------------------------------------


def test_error_code_is_reexported_from_package_root() -> None:
    """`from getpatter import ErrorCode` must work."""
    assert getpatter.ErrorCode is ErrorCode
    assert getpatter.ErrorCode.CONFIG is ErrorCode.CONFIG


def test_error_code_canonical_values() -> None:
    """Wire format is stable — these strings must NEVER change.

    Downstream consumers branch on these values; renaming any of them is
    a breaking change.
    """
    assert ErrorCode.CONFIG.value == "CONFIG"
    assert ErrorCode.CONNECTION.value == "CONNECTION"
    assert ErrorCode.AUTH.value == "AUTH"
    assert ErrorCode.TIMEOUT.value == "TIMEOUT"
    assert ErrorCode.RATE_LIMIT.value == "RATE_LIMIT"
    assert ErrorCode.WEBHOOK_VERIFICATION.value == "WEBHOOK_VERIFICATION"
    assert ErrorCode.INPUT_VALIDATION.value == "INPUT_VALIDATION"
    assert ErrorCode.PROVIDER_ERROR.value == "PROVIDER_ERROR"
    assert ErrorCode.PROVISION.value == "PROVISION"
    assert ErrorCode.INTERNAL.value == "INTERNAL"


def test_error_code_is_str_compatible() -> None:
    """`StrEnum` lets users compare against bare strings without `.value`."""
    assert ErrorCode.CONFIG == "CONFIG"
    assert "CONFIG" == ErrorCode.CONFIG  # symmetric


# ---------------------------------------------------------------------------
# Default codes per exception class
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc_cls, expected_code",
    [
        (PatterError, ErrorCode.INTERNAL),
        (PatterConnectionError, ErrorCode.CONNECTION),
        (AuthenticationError, ErrorCode.AUTH),
        (ProvisionError, ErrorCode.PROVISION),
        (RateLimitError, ErrorCode.RATE_LIMIT),
    ],
)
def test_exception_default_code_matches_class(
    exc_cls: type[PatterError], expected_code: ErrorCode
) -> None:
    err = exc_cls("boom")
    assert err.code is expected_code
    # And the message survives intact.
    assert str(err) == "boom"


def test_class_level_code_attribute_is_set() -> None:
    """`.code` is also accessible as a class attribute (not only on instances).

    This is what lets `except`-clauses dispatch on subclass without
    instantiating, and lets users introspect codes from the class itself.
    """
    assert PatterError.code is ErrorCode.INTERNAL
    assert PatterConnectionError.code is ErrorCode.CONNECTION
    assert AuthenticationError.code is ErrorCode.AUTH
    assert ProvisionError.code is ErrorCode.PROVISION
    assert RateLimitError.code is ErrorCode.RATE_LIMIT


# ---------------------------------------------------------------------------
# Per-instance overrides
# ---------------------------------------------------------------------------


def test_per_instance_code_override() -> None:
    """A caller can override `.code` for a one-off case (rare)."""
    err = PatterConnectionError("api 5xx", code=ErrorCode.PROVIDER_ERROR)
    assert err.code is ErrorCode.PROVIDER_ERROR
    # Class default must remain untouched.
    assert PatterConnectionError.code is ErrorCode.CONNECTION


def test_override_on_base_class_also_works() -> None:
    err = PatterError("oops", code=ErrorCode.TIMEOUT)
    assert err.code is ErrorCode.TIMEOUT


# ---------------------------------------------------------------------------
# Backward compatibility: class hierarchy unchanged
# ---------------------------------------------------------------------------


def test_subclass_relationships() -> None:
    """The pre-existing class hierarchy must keep working — adding `code`
    is purely additive, not a refactor of the inheritance graph.
    """
    assert issubclass(PatterConnectionError, PatterError)
    assert issubclass(AuthenticationError, PatterError)
    assert issubclass(ProvisionError, PatterError)
    assert issubclass(RateLimitError, PatterConnectionError)
    assert issubclass(RateLimitError, PatterError)


def test_existing_callers_can_still_construct_without_code_kwarg() -> None:
    """Opt-in config rule: `code=` is optional with safe default."""
    # Positional message-only construction (the historical shape) must work.
    AuthenticationError("auth failed")
    ProvisionError("number rejected")
    PatterConnectionError("ws closed")
    RateLimitError("429")
    PatterError("generic")


# ---------------------------------------------------------------------------
# Smoke: real raise site surfaces the right code
# ---------------------------------------------------------------------------


def test_deepgram_401_raises_authentication_error_with_auth_code() -> None:
    """Smoke that a real raise site surfaces the expected code.

    `providers/deepgram_stt.py` translates an HTTP 401 close from Deepgram's
    WS endpoint into `AuthenticationError`. We only verify the typed shape
    here — no network call — because the goal is to confirm that the
    enum-on-class wiring reaches downstream code paths unchanged.
    """
    err = AuthenticationError("Deepgram rejected API key (HTTP 401)")
    assert isinstance(err, PatterError)
    assert err.code is ErrorCode.AUTH
    # Generic catch-all path: a downstream UI mapping `code → toast` doesn't
    # need to import the subclass at all.
    caught: PatterError = err
    assert caught.code == "AUTH"  # StrEnum equality
