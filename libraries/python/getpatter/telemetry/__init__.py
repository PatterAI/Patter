"""Anonymous usage telemetry for Patter (opt-out, default ON).

This module lets the Patter maintainers see *coarse, anonymous* facts about how
the SDK is used (which engines / providers / carriers, on which platforms, at
which versions). It is intentionally separate from ``getpatter.observability``,
which is user-facing OpenTelemetry tracing the *user* points at their *own*
backend — a different concern with different data and a different destination.

Privacy posture:

* **No PII, ever.** No phone numbers, transcripts, audio, prompts, tool
  arguments, API keys, customer identifiers, file paths, hostnames, or IPs.
* **Anonymous.** A per-process random run id (regenerated each run), never a
  hardware fingerprint. The collector drops the source IP.
* **Opt-out.** On by default; disable with ``PATTER_TELEMETRY_DISABLED=1``,
  ``DO_NOT_TRACK=1``, or ``Patter(telemetry=False)``. Auto-disabled in CI/tests.
* **Fail-safe.** Fire-and-forget, bounded buffer, short timeouts, all errors
  swallowed — it can never block or break a live phone call.

Inspect exactly what would be sent without sending it: ``PATTER_TELEMETRY_DEBUG=1``.
"""

from getpatter.telemetry.client import DEFAULT_ENDPOINT, TelemetryClient
from getpatter.telemetry.consent import is_enabled
from getpatter.telemetry.events import (
    EVENT_AGENT_CONFIGURED,
    EVENT_CALL_COMPLETED,
    EVENT_CALL_STARTED,
    EVENT_CLI_COMMAND,
    EVENT_FEATURE_USED,
    EVENT_FIRST_RUN,
    EVENT_SDK_INITIALIZED,
    SCHEMA_VERSION,
    build_event,
)

__all__ = [
    "TelemetryClient",
    "DEFAULT_ENDPOINT",
    "is_enabled",
    "build_event",
    "SCHEMA_VERSION",
    "EVENT_SDK_INITIALIZED",
    "EVENT_FIRST_RUN",
    "EVENT_CLI_COMMAND",
    "EVENT_FEATURE_USED",
    "EVENT_AGENT_CONFIGURED",
    "EVENT_CALL_STARTED",
    "EVENT_CALL_COMPLETED",
]
