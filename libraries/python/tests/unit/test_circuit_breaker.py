"""Unit tests for getpatter.tools.circuit_breaker.

Parity with libraries/typescript/tests/circuit-breaker.test.ts.
"""

from __future__ import annotations

import warnings

import pytest

from getpatter.tools.circuit_breaker import (
    CircuitBreakerOptions,
    CircuitBreakerRegistry,
    CircuitBreakerState,
)


class _FakeClock:
    """Deterministic clock — tests advance ``now`` explicitly so they
    finish in milliseconds and survive loaded CI runners."""

    def __init__(self, initial: float = 1_000_000.0) -> None:
        self._t = initial

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


class TestCircuitBreaker:
    def test_starts_closed_and_allows_first_call(self) -> None:
        breaker = CircuitBreakerRegistry()
        assert breaker.allow("book_appointment") is True
        assert breaker.snapshot("book_appointment") is None

    def test_stays_closed_on_success(self) -> None:
        breaker = CircuitBreakerRegistry(CircuitBreakerOptions(failure_threshold=3))
        breaker.record_success("book")
        assert breaker.allow("book") is True
        snap = breaker.snapshot("book")
        assert snap is None or snap.consecutive_failures == 0

    def test_opens_after_threshold_consecutive_failures(self) -> None:
        clock = _FakeClock()
        breaker = CircuitBreakerRegistry(
            CircuitBreakerOptions(failure_threshold=3, cooldown_ms=5_000),
            clock=clock.now,
        )
        breaker.record_failure("book")
        breaker.record_failure("book")
        assert breaker.allow("book") is True  # 2 < 3 still closed
        breaker.record_failure("book")
        assert breaker.allow("book") is False
        snap = breaker.snapshot("book")
        assert snap is not None
        assert snap.state == CircuitBreakerState.OPEN

    def test_resets_to_closed_on_success_after_failures(self) -> None:
        breaker = CircuitBreakerRegistry(CircuitBreakerOptions(failure_threshold=3))
        breaker.record_failure("book")
        breaker.record_failure("book")
        breaker.record_success("book")
        breaker.record_failure("book")
        breaker.record_failure("book")
        assert breaker.allow("book") is True

    def test_open_to_half_open_after_cooldown(self) -> None:
        clock = _FakeClock()
        breaker = CircuitBreakerRegistry(
            CircuitBreakerOptions(failure_threshold=2, cooldown_ms=10_000),
            clock=clock.now,
        )
        breaker.record_failure("book")
        breaker.record_failure("book")
        assert breaker.allow("book") is False

        clock.advance(9.999)
        assert breaker.allow("book") is False  # still in cooldown

        clock.advance(0.002)  # total 10.001 ≥ cooldown
        assert breaker.allow("book") is True
        snap = breaker.snapshot("book")
        assert snap is not None
        assert snap.state == CircuitBreakerState.HALF_OPEN

    def test_half_open_to_closed_on_probe_success(self) -> None:
        clock = _FakeClock()
        breaker = CircuitBreakerRegistry(
            CircuitBreakerOptions(failure_threshold=2, cooldown_ms=1_000),
            clock=clock.now,
        )
        breaker.record_failure("book")
        breaker.record_failure("book")
        clock.advance(1.001)
        assert breaker.allow("book") is True
        breaker.record_success("book")
        snap = breaker.snapshot("book")
        assert snap is not None
        assert snap.state == CircuitBreakerState.CLOSED
        assert snap.consecutive_failures == 0

    def test_half_open_to_open_on_probe_failure(self) -> None:
        clock = _FakeClock()
        breaker = CircuitBreakerRegistry(
            CircuitBreakerOptions(failure_threshold=2, cooldown_ms=1_000),
            clock=clock.now,
        )
        breaker.record_failure("book")
        breaker.record_failure("book")
        clock.advance(1.001)
        assert breaker.allow("book") is True  # probe permitted
        breaker.record_failure("book")  # probe failed
        assert breaker.allow("book") is False  # back to OPEN

    def test_threshold_zero_disables(self) -> None:
        breaker = CircuitBreakerRegistry(
            CircuitBreakerOptions(failure_threshold=0, cooldown_ms=1_000)
        )
        for _ in range(100):
            breaker.record_failure("book")
        assert breaker.allow("book") is True

    def test_per_tool_independence(self) -> None:
        breaker = CircuitBreakerRegistry(CircuitBreakerOptions(failure_threshold=2))
        breaker.record_failure("a")
        breaker.record_failure("a")
        assert breaker.allow("a") is False
        assert breaker.allow("b") is True

    def test_time_until_half_open(self) -> None:
        clock = _FakeClock()
        breaker = CircuitBreakerRegistry(
            CircuitBreakerOptions(failure_threshold=2, cooldown_ms=5_000),
            clock=clock.now,
        )
        # ``time_until_half_open`` keeps returning seconds for back-compat;
        # ``time_until_half_open_ms`` is the TS-parity variant.
        assert breaker.time_until_half_open("book") == 0.0
        assert breaker.time_until_half_open_ms("book") == 0.0
        breaker.record_failure("book")
        breaker.record_failure("book")
        assert breaker.time_until_half_open("book") == 5.0
        assert breaker.time_until_half_open_ms("book") == 5_000.0
        clock.advance(2.0)
        assert breaker.time_until_half_open("book") == 3.0
        assert breaker.time_until_half_open_ms("book") == 3_000.0


class TestCooldownSDeprecation:
    """Regression tests for the backward-compatible ``cooldown_s=`` shim."""

    def test_legacy_cooldown_s_kwarg_still_works(self) -> None:
        """``CircuitBreakerOptions(cooldown_s=30.0)`` is converted to
        ``cooldown_ms=30_000`` so existing callers keep working through
        v0.7.0 — the version where the seconds shim is scheduled to be
        removed."""
        # Reset the once-per-process flag so the warning fires for this test
        # regardless of test execution order.
        from getpatter.tools import circuit_breaker as cb_module

        cb_module._warned_cooldown_s = False

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", DeprecationWarning)
            opts = CircuitBreakerOptions(failure_threshold=3, cooldown_s=30.0)

        assert opts.cooldown_ms == 30_000
        # Warning must fire and mention the old name + the new name + the
        # removal version so users have everything they need to migrate.
        dep = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert len(dep) >= 1
        msg = str(dep[0].message)
        assert "cooldown_s" in msg
        assert "cooldown_ms" in msg
        assert "v0.7.0" in msg

    def test_legacy_cooldown_s_drives_breaker_behaviour(self) -> None:
        """The seconds-based shim must produce the same runtime behaviour
        as the millisecond field — proves the conversion is correct, not
        just stored."""
        from getpatter.tools import circuit_breaker as cb_module

        cb_module._warned_cooldown_s = False

        clock = _FakeClock()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            breaker = CircuitBreakerRegistry(
                CircuitBreakerOptions(failure_threshold=2, cooldown_s=10.0),
                clock=clock.now,
            )
        breaker.record_failure("book")
        breaker.record_failure("book")
        assert breaker.allow("book") is False
        clock.advance(9.999)
        assert breaker.allow("book") is False
        clock.advance(0.002)
        assert breaker.allow("book") is True

    def test_explicit_cooldown_ms_wins_over_legacy_cooldown_s(self) -> None:
        """If both are passed, ``cooldown_ms`` wins — the explicit new
        name is always authoritative."""
        from getpatter.tools import circuit_breaker as cb_module

        cb_module._warned_cooldown_s = False

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            opts = CircuitBreakerOptions(cooldown_ms=7_500, cooldown_s=42.0)
        assert opts.cooldown_ms == 7_500


@pytest.mark.parametrize(
    "default_field, default_value",
    [("cooldown_ms", 30_000), ("failure_threshold", 5)],
)
def test_defaults_match_typescript(default_field: str, default_value: int) -> None:
    """Defaults must match the TypeScript SDK byte-for-byte —
    ``DEFAULT_COOLDOWN_MS = 30_000`` and ``DEFAULT_FAILURE_THRESHOLD = 5``
    in ``libraries/typescript/src/tools/circuit-breaker.ts``."""
    opts = CircuitBreakerOptions()
    assert getattr(opts, default_field) == default_value
