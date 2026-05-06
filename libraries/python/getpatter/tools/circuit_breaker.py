"""Per-tool circuit breaker for the Patter SDK (Python parity with TS
``libraries/typescript/src/tools/circuit-breaker.ts``).

Trips OPEN after N consecutive failures, rejects calls for a cooldown
window so a flaky downstream (DB outage, vendor API rate-limit, dead
webhook) doesn't burn LLM tokens on retries that will keep failing.
After the cooldown elapses the next call probes (HALF_OPEN); a success
resets to CLOSED, a failure reopens. The model receives a structured
``{"error": ..., "fallback": True}`` payload in all rejection paths so
it can recover gracefully instead of waiting forever.

Lightweight in-memory implementation — one ``CircuitBreakerRegistry``
per tool executor, state is per tool name. Not persisted across process
restarts (intentional — voice calls are too short for persistence to
matter).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

#: Default consecutive-failure threshold that flips CLOSED → OPEN.
DEFAULT_FAILURE_THRESHOLD = 5
#: Default time (seconds) the breaker stays OPEN before allowing a probe.
DEFAULT_COOLDOWN_S = 30.0


class CircuitBreakerState(str, Enum):
    """Lifecycle states for the breaker."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class _PerToolState:
    state: CircuitBreakerState = CircuitBreakerState.CLOSED
    consecutive_failures: int = 0
    opened_at: float = 0.0


@dataclass
class CircuitBreakerOptions:
    """Tunables for a single per-tool breaker."""

    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD
    cooldown_s: float = DEFAULT_COOLDOWN_S


class CircuitBreakerRegistry:
    """Per-name registry tracking circuit state for a fleet of tools."""

    def __init__(
        self,
        opts: CircuitBreakerOptions | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        opts = opts or CircuitBreakerOptions()
        self._threshold = opts.failure_threshold
        self._cooldown_s = opts.cooldown_s
        self._state: dict[str, _PerToolState] = {}
        # Inject for deterministic tests; defaults to ``time.monotonic``.
        self._clock = clock or time.monotonic

    def allow(self, tool_name: str) -> bool:
        """Return ``True`` when this tool is currently allowed to run."""
        if self._threshold <= 0:
            return True
        s = self._state.get(tool_name)
        if s is None:
            return True
        if s.state == CircuitBreakerState.CLOSED:
            return True
        if s.state == CircuitBreakerState.OPEN:
            if self._clock() - s.opened_at >= self._cooldown_s:
                # Cooldown elapsed — allow exactly one probe to determine
                # if the downstream has recovered.
                s.state = CircuitBreakerState.HALF_OPEN
                return True
            return False
        # HALF_OPEN — allow only one in-flight probe at a time.
        return True

    def record_success(self, tool_name: str) -> None:
        """Mark a successful execution. Resets the breaker to CLOSED."""
        s = self._state.get(tool_name)
        if s is None:
            return
        s.state = CircuitBreakerState.CLOSED
        s.consecutive_failures = 0
        s.opened_at = 0.0

    def record_failure(self, tool_name: str) -> None:
        """Mark a failed execution; trips OPEN once threshold is reached."""
        if self._threshold <= 0:
            return
        s = self._state.get(tool_name)
        if s is None:
            s = _PerToolState()
            self._state[tool_name] = s
        s.consecutive_failures += 1
        if s.consecutive_failures >= self._threshold:
            s.state = CircuitBreakerState.OPEN
            s.opened_at = self._clock()

    def time_until_half_open(self, tool_name: str) -> float:
        """Time until OPEN → HALF_OPEN, in seconds. Returns ``0`` when
        the breaker is currently allowing calls."""
        s = self._state.get(tool_name)
        if s is None or s.state != CircuitBreakerState.OPEN:
            return 0.0
        elapsed = self._clock() - s.opened_at
        return max(0.0, self._cooldown_s - elapsed)

    def snapshot(self, tool_name: str) -> _PerToolState | None:
        """Snapshot for debugging / metrics."""
        s = self._state.get(tool_name)
        if s is None:
            return None
        return _PerToolState(
            state=s.state,
            consecutive_failures=s.consecutive_failures,
            opened_at=s.opened_at,
        )
