/**
 * Anonymous usage telemetry for Patter (opt-out, default ON).
 *
 * Lets the Patter maintainers see *coarse, anonymous* facts about how the SDK is
 * used (which engines / providers / carriers, on which platforms, at which
 * versions). Separate from `src/observability` (user-facing OpenTelemetry the
 * user points at their own backend) — a different concern, data, and destination.
 *
 * Privacy posture: no PII ever; a per-process random run id (never a hardware
 * fingerprint; the collector drops the source IP); opt-out (on by default,
 * disable with `PATTER_TELEMETRY_DISABLED=1`, `DO_NOT_TRACK=1`, or
 * `telemetry: false`; auto-disabled in CI/tests); fail-safe fire-and-forget so
 * it can never block or break a live call. Inspect-without-send: `PATTER_TELEMETRY_DEBUG=1`.
 */

export { TelemetryClient, DEFAULT_ENDPOINT } from './client';
export type { TelemetryClientOptions } from './client';
export { isEnabled } from './consent';
export {
  buildEvent,
  SCHEMA_VERSION,
  EVENT_SDK_INITIALIZED,
  EVENT_FIRST_RUN,
  EVENT_CLI_COMMAND,
  EVENT_FEATURE_USED,
  EVENT_AGENT_CONFIGURED,
  EVENT_CALL_STARTED,
  EVENT_CALL_COMPLETED,
} from './events';
export type { TelemetryEvent, Dimensions } from './events';
export { recordCallCompleted, recordCallStarted } from './call-metrics';
