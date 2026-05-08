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
import warnings
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

#: Default consecutive-failure threshold that flips CLOSED → OPEN.
DEFAULT_FAILURE_THRESHOLD = 5
#: Default time (milliseconds) the breaker stays OPEN before allowing a
#: probe. Matches the TypeScript ``DEFAULT_COOLDOWN_MS`` constant —
#: aligning the unit prevents the "30 vs 30000" copy-paste foot-gun
#: between SDKs.
DEFAULT_COOLDOWN_MS = 30_000


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


# Once-per-process flag so the deprecation warning fires once instead of
# on every call — keeps logs readable when callers wire the breaker into
# hot paths.
_warned_cooldown_s: bool = False


@dataclass
class CircuitBreakerOptions:
    """Tunables for a single per-tool breaker.

    Field naming is millisecond-based to match the TypeScript SDK
    (``cooldownMs``) and the broader Patter convention for time fields
    (``silence_duration_ms``, ``prefix_padding_ms``, ...).

    .. deprecated::
        Passing ``cooldown_s`` (seconds) as a constructor kwarg is
        accepted with a ``DeprecationWarning`` for backward compatibility
        and converted internally to ``cooldown_ms``. Scheduled for
        removal in v0.7.0.
    """

    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD
    cooldown_ms: int = DEFAULT_COOLDOWN_MS


# Capture the dataclass-generated __init__ so we can wrap it to accept
# the legacy ``cooldown_s`` kwarg without polluting the public field set.
_CBO_DC_INIT = CircuitBreakerOptions.__init__


def _cbo_init(
    self: CircuitBreakerOptions,
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
    cooldown_ms: int | None = None,
    *,
    cooldown_s: float | None = None,
    **_kwargs: Any,
) -> None:
    """Wrap the dataclass __init__ so legacy ``cooldown_s=`` keeps
    working with a one-shot ``DeprecationWarning``. An explicit
    ``cooldown_ms`` always wins; ``cooldown_s`` is converted only when
    ``cooldown_ms`` is unset."""
    if cooldown_s is not None:
        global _warned_cooldown_s
        if not _warned_cooldown_s:
            warnings.warn(
                "CircuitBreakerOptions(cooldown_s=...) is deprecated and "
                "will be removed in v0.7.0. Use cooldown_ms (milliseconds) "
                "to match the TypeScript SDK and the rest of the Patter "
                "time-field convention.",
                DeprecationWarning,
                stacklevel=2,
            )
            _warned_cooldown_s = True
        if cooldown_ms is None:
            cooldown_ms = int(cooldown_s * 1000)
    if cooldown_ms is None:
        cooldown_ms = DEFAULT_COOLDOWN_MS
    _CBO_DC_INIT(
        self,
        failure_threshold=failure_threshold,
        cooldown_ms=cooldown_ms,
    )


CircuitBreakerOptions.__init__ = _cbo_init  # type: ignore[method-assign]


class CircuitBreakerRegistry:
    """Per-name registry tracking circuit state for a fleet of tools.

    Internal time accounting is in **seconds** (``time.monotonic``)
    because the injected ``clock`` callable is expected to return
    seconds — both for backward compatibility with existing test
    fixtures and because Python time APIs are seconds-based by
    convention. The public ``cooldown_ms`` field is converted to
    seconds once at construction.
    """

    def __init__(
        self,
        opts: CircuitBreakerOptions | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        opts = opts or CircuitBreakerOptions()
        self._threshold = opts.failure_threshold
        # Convert public ms field → internal seconds at the boundary so
        # the rest of the code (and the user-injected clock) keep using
        # seconds.
        self._cooldown_s: float = opts.cooldown_ms / 1000.0
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
        """Time until OPEN → HALF_OPEN, in **seconds**. Returns ``0``
        when the breaker is currently allowing calls.

        Kept seconds-based for backward compatibility — the executor
        multiplies by 1000 to populate the ``retry_after_ms`` field on
        the rejection JSON. New callers that want a TypeScript-parity
        millisecond return value should use :meth:`time_until_half_open_ms`.
        """
        s = self._state.get(tool_name)
        if s is None or s.state != CircuitBreakerState.OPEN:
            return 0.0
        elapsed = self._clock() - s.opened_at
        return max(0.0, self._cooldown_s - elapsed)

    def time_until_half_open_ms(self, tool_name: str) -> float:
        """Millisecond-returning variant of :meth:`time_until_half_open`,
        matching the TypeScript ``timeUntilHalfOpen`` signature."""
        return self.time_until_half_open(tool_name) * 1000.0

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
