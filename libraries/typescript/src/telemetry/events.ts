/**
 * Telemetry event schema and allowlist enforcement.
 *
 * Two layers of defense run before any event is built:
 *  - Key allowlist (`ALLOWED_DIMENSIONS`): a dimension whose key is not listed is
 *    dropped.
 *  - Value allowlist (`DIMENSION_VALUES`): for every enum dimension, a value not
 *    on its closed set is coerced to `"other"` — making a raw custom tool name,
 *    custom integration id, or unexpected string structurally impossible to emit,
 *    even from a buggy caller. The relay re-validates both layers server-side.
 *
 * NEVER add a field that could carry PII or call content: phone numbers, call
 * SIDs, transcripts, audio, prompts, tool arguments, API keys, customer
 * identifiers, file paths, hostnames, IPs, or free text.
 *
 * Bump `SCHEMA_VERSION` whenever the event/field/value set changes so the relay
 * can apply version-appropriate validation. Mirrors `getpatter/telemetry/events.py`.
 */

import * as os from 'node:os';

import { isCi, isTest } from './env';
import { installId, runId } from './install-id';
import { STACK_VENDORS } from './stack';

export const SCHEMA_VERSION = 8;

export const EVENT_SDK_INITIALIZED = 'sdk_initialized';
export const EVENT_FIRST_RUN = 'first_run';
export const EVENT_CLI_COMMAND = 'cli_command';
export const EVENT_FEATURE_USED = 'feature_used';
export const EVENT_AGENT_CONFIGURED = 'agent_configured';
export const EVENT_CALL_STARTED = 'call_started';
export const EVENT_CALL_COMPLETED = 'call_completed';
// Activation-blocker signal: emitted once when required runtime config is missing
// so the instance cannot proceed. In the NEVER-sampled set (always delivered).
export const EVENT_CONFIG_INCOMPLETE = 'config_incomplete';

const ALLOWED_EVENTS = new Set<string>([
  EVENT_SDK_INITIALIZED,
  EVENT_FIRST_RUN,
  EVENT_CLI_COMMAND,
  EVENT_FEATURE_USED,
  EVENT_AGENT_CONFIGURED,
  EVENT_CALL_STARTED,
  EVENT_CALL_COMPLETED,
  EVENT_CONFIG_INCOMPLETE,
]);

/**
 * Every enum dimension maps to its closed value set. Any value not in the set is
 * coerced to `"other"`. Keep byte-for-byte identical to `DIMENSION_VALUES` in the
 * Python `events.py`.
 */
export const DIMENSION_VALUES: Record<string, ReadonlySet<string>> = {
  carrier: new Set(['twilio', 'telnyx', 'plivo', 'none']),
  tunnel: new Set(['static', 'configured', 'none']),
  engine: new Set(['realtime', 'convai', 'pipeline']),
  provider: new Set([
    'openai',
    'elevenlabs',
    'deepgram',
    'cartesia',
    'cerebras',
    'anthropic',
    'google',
    'whisper',
    'other',
  ]),
  // agent_configured dimensions
  custom_tool_count_bucket: new Set(['0', '1', '2_3', '4_6', '7_12', '13_plus']),
  integration: new Set(['openclaw', 'mcp', 'hermes', 'other', 'none']),
  integration_kind: new Set(['consult', 'mcp', 'none']),
  mcp_server_count_bucket: new Set(['0', '1', '2_3', '4_plus']),
  // call_started / call_completed: inbound vs outbound — a core usage split.
  direction: new Set(['inbound', 'outbound', 'none']),
  // cli_command: which CLI subcommand was invoked (never args/flags values).
  cli_command: new Set(['dashboard', 'eval', 'hermes', 'openclaw', 'telemetry', 'none', 'other']),
  // call_completed: the call's terminal outcome
  outcome: new Set(['completed', 'error', 'no_answer', 'busy', 'failed']),
  // call_completed: terminal error code (mirrors ErrorCode, plus "other"). Never
  // the error message.
  error_code: new Set([
    'config',
    'connection',
    'auth',
    'timeout',
    'rate_limit',
    'webhook_verification',
    'input_validation',
    'provider_error',
    'provision',
    'internal',
    'other',
  ]),
  // feature_used (pipeline): per-layer vendor of the composed stack. A
  // providerKey not on the closed allowlist collapses to "other"; an absent layer
  // is omitted (the value set keeps "none" only as a safety token).
  stt_provider: new Set([...STACK_VENDORS, 'none']),
  tts_provider: new Set([...STACK_VENDORS, 'none']),
  llm_provider: new Set([...STACK_VENDORS, 'none']),
  // sdk_initialized: anonymous deploy-shape (presence-only env/file probes).
  invoked_by_agent: new Set(['claude', 'cursor', 'copilot', 'gemini', 'windsurf', 'other', 'none']),
  serverless: new Set(['lambda', 'cloud_run', 'vercel', 'azure_functions', 'none']),
  cloud: new Set(['aws', 'gcp', 'azure', 'fly', 'none']),
  package_manager: new Set(['npm', 'pnpm', 'yarn', 'bun', 'pip', 'uv', 'poetry', 'pipenv', 'conda', 'none']),
  days_since_install_bucket: new Set(['0', '1_7', '8_30', '30_plus']),
  // agent_configured: feature-adoption (Realtime tuning).
  noise_reduction: new Set(['near_field', 'far_field', 'none']),
  turn_detection: new Set(['default', 'custom', 'none']),
  // call_completed: how many conversational turns the call had.
  turn_count_bucket: new Set(['0', '1', '2_3', '4_6', '7_12', '13_plus']),
  // config_incomplete: coarse category of the absent required runtime config
  // blocking activation. Never the key value, env var name, or any PII.
  missing: new Set(['carrier_credentials', 'llm_key', 'engine_config', 'other']),
  // call_completed: which layer the terminal error originated in (derived from
  // CallMetrics.errorCode/outcome). "none" when the call ended cleanly.
  error_layer: new Set(['stt', 'llm', 'tts', 'carrier', 'config', 'internal', 'none', 'other']),
  // call_completed: coarse reason the call ended (derived from terminal
  // outcome/error/AMD state). hangup_local/hangup_remote only when the side is
  // reliably known; else fall back to completed (clean end) or other.
  disconnect_reason: new Set([
    'hangup_local',
    'hangup_remote',
    'error',
    'timeout',
    'no_answer',
    'busy',
    'completed',
    'other',
  ]),
  // call_started / call_completed: coarse age of the install at call time
  // (install-id file mtime vs now). "unknown" when the timestamp can't be read.
  time_to_first_call_bucket: new Set(['lt_1h', '1h_1d', '1d_7d', 'gt_7d', 'unknown']),
};

// Numeric dimensions pass through without value coercion. latency_ms (whole ms)
// and duration_seconds (whole seconds) are raw operational metrics. The per-stage
// call_completed latencies (stt_latency_ms, llm_ttft_ms, tts_first_byte_ms,
// eou_latency_ms) are read off the existing CallMetrics latency breakdown — whole
// ints, omitted when the stage didn't run. sample_rate is the client-side
// sampling rate in (0,1] for high-frequency call events (weight = 1/sample_rate).
const NUMERIC_DIMENSIONS = new Set<string>([
  'builtin_tool_count',
  'latency_ms',
  'duration_seconds',
  'cost_usd',
  'stt_latency_ms',
  'llm_ttft_ms',
  'tts_first_byte_ms',
  'eou_latency_ms',
  'sample_rate',
]);
// feature_used (pipeline): sanitized per-layer model token (e.g.
// "deepgram-nova-3", "anthropic-claude-haiku-4-5"). NOT a closed enum — produced
// by stack.modelToken, which already coerces anything PII-risky to
// "{vendor}-other". buildEvent re-checks the shape; the relay mirrors it.
// String dimensions are validated by a safe-shape regex (model tokens + the
// `previous_sdk_version` version string for the upgrade funnel; the regex also
// matches versions like `0.6.3`).
const STRING_DIMENSIONS = new Set<string>([
  'stt_model',
  'tts_model',
  'llm_model',
  'previous_sdk_version',
]);
const MODEL_TOKEN_RE = /^[a-z0-9][a-z0-9.-]{0,40}$/;
// Boolean dimensions: a feature is on/off. Kept only if the value is a real bool.
const BOOL_DIMENSIONS = new Set<string>([
  'container',
  'preambles_used',
  'per_tool_timeouts_set',
  'llm_fallback_configured',
]);
// ID dimensions: a random SDK-generated per-call correlation id — never the
// carrier call SID. The hex32 shape is enforced here as defense in depth (the
// relay re-checks); a value that fails the shape is DROPPED, never coerced.
const ID_RE = /^[0-9a-f]{32}$/;
const ID_DIMENSIONS = new Set<string>(['call_uid']);
const ALLOWED_DIMENSIONS = new Set<string>([
  ...Object.keys(DIMENSION_VALUES),
  ...NUMERIC_DIMENSIONS,
  ...STRING_DIMENSIONS,
  ...BOOL_DIMENSIONS,
  ...ID_DIMENSIONS,
]);

export type Scalar = string | number | boolean;
export type TelemetryEvent = Record<string, Scalar>;
export type Dimensions = Record<string, Scalar | null | undefined>;

function osFamily(): string {
  const p = os.platform(); // 'darwin' | 'linux' | 'win32' | ...
  if (p === 'win32') return 'windows';
  return p || 'unknown';
}

function arch(): string {
  // os.arch() returns Node's own token vocabulary ('x64' | 'arm64' | ...), a
  // disjoint set from Python's platform.machine() — normalised to the same output.
  const a = os.arch();
  if (a === 'x64') return 'x86_64';
  if (a === 'arm64') return 'arm64';
  return 'other'; // keep arch low-cardinality (no raw uname strings on the wire)
}

function runtimeVersion(): string {
  // Bucket Node version to major.minor (no patch — avoid fingerprinting).
  const parts = (process.versions.node ?? '0.0').split('.');
  return `${parts[0] ?? '0'}.${parts[1] ?? '0'}`;
}

/**
 * Build a telemetry event payload containing only allowlisted fields/values.
 * Throws for an unknown event name (a programmer error the caller swallows).
 * Unknown dimension keys are dropped; off-list enum values are coerced to `"other"`.
 */
export function buildEvent(
  name: string,
  opts: { sdkVersion: string; dimensions?: Dimensions },
): TelemetryEvent {
  if (!ALLOWED_EVENTS.has(name)) {
    throw new Error(`unknown telemetry event: ${name}`);
  }

  const event: TelemetryEvent = {
    event: name,
    schema_version: SCHEMA_VERSION,
    run_id: runId(),
    install_id: installId(),
    sdk: 'typescript',
    sdk_version: opts.sdkVersion,
    os: osFamily(),
    arch: arch(),
    runtime: 'node',
    runtime_version: runtimeVersion(),
    ci: isCi() || isTest(),
  };

  for (const [key, raw] of Object.entries(opts.dimensions ?? {})) {
    if (!ALLOWED_DIMENSIONS.has(key) || raw === null || raw === undefined) {
      continue;
    }
    let value: Scalar = raw;
    const allowed = DIMENSION_VALUES[key];
    if (allowed && !(typeof value === 'string' && allowed.has(value))) {
      value = 'other'; // off-list enum value can never reach the wire raw
    } else if (STRING_DIMENSIONS.has(key)) {
      // Sanitized model / version token: enforce the safe shape; drop anything
      // else (the SDK already guarantees this, but never trust input).
      if (!(typeof value === 'string' && MODEL_TOKEN_RE.test(value))) {
        continue;
      }
    } else if (ID_DIMENSIONS.has(key)) {
      // Random SDK-generated per-call correlation id — never the carrier call
      // SID; hex32 shape enforced as defense in depth, relay re-checks. Keep
      // only a string matching ID_RE; anything else is DROPPED, never coerced.
      if (!(typeof value === 'string' && ID_RE.test(value))) {
        continue;
      }
    } else if (BOOL_DIMENSIONS.has(key) && typeof value !== 'boolean') {
      continue;
    } else if (NUMERIC_DIMENSIONS.has(key) && typeof value !== 'number') {
      // Numeric dims must be numbers: a buggy caller passing a string under
      // latency_ms/cost_usd would otherwise ship free text to the wire —
      // the one gap in the two-layer allowlist. Mirrors Python.
      continue;
    }
    if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
      event[key] = value;
    }
  }

  return event;
}
