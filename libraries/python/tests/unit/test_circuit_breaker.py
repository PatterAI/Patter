"""Unit tests for getpatter.tools.circuit_breaker.

Parity with libraries/typescript/tests/circuit-breaker.test.ts.
"""

from __future__ import annotations

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
            CircuitBreakerOptions(failure_threshold=3, cooldown_s=5.0),
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
            CircuitBreakerOptions(failure_threshold=2, cooldown_s=10.0),
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
            CircuitBreakerOptions(failure_threshold=2, cooldown_s=1.0),
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
            CircuitBreakerOptions(failure_threshold=2, cooldown_s=1.0),
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
            CircuitBreakerOptions(failure_threshold=0, cooldown_s=1.0)
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
            CircuitBreakerOptions(failure_threshold=2, cooldown_s=5.0),
            clock=clock.now,
        )
        assert breaker.time_until_half_open("book") == 0.0
        breaker.record_failure("book")
        breaker.record_failure("book")
        assert breaker.time_until_half_open("book") == 5.0
        clock.advance(2.0)
        assert breaker.time_until_half_open("book") == 3.0
