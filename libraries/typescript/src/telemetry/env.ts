/**
 * Environment detection used to suppress telemetry in CI and test runs.
 *
 * Telemetry is opt-out (on by default), but we never phone home from a CI job or
 * a test runner: those machines are not consenting humans and would skew the
 * aggregate usage numbers. Detection is best-effort and intentionally broad.
 * Mirrors `getpatter/telemetry/env.py`.
 */

const CI_ENV_VARS = [
  'CI',
  'CONTINUOUS_INTEGRATION',
  'GITHUB_ACTIONS',
  'GITLAB_CI',
  'TRAVIS',
  'CIRCLECI',
  'APPVEYOR',
  'TF_BUILD',
  'TEAMCITY_VERSION',
  'BUILDKITE',
  'DRONE',
  'JENKINS_URL',
  'HUDSON_URL',
  'BAMBOO_BUILDKEY',
  'CODEBUILD_BUILD_ID',
] as const;

const TEST_ENV_VARS = ['VITEST', 'JEST_WORKER_ID'] as const;

/** True when `value` is set to anything other than empty / a falsey literal. */
export function isTruthy(value: string | undefined): boolean {
  if (value === undefined) return false;
  const v = value.trim().toLowerCase();
  return v !== '' && v !== '0' && v !== 'false' && v !== 'no' && v !== 'off';
}

/**
 * Client-side sampling rate for high-frequency call events, in `[0, 1]`.
 *
 * Read from `PATTER_TELEMETRY_SAMPLE` and clamped to `[0, 1]`. Defaults to `1.0`
 * (no sampling — every event kept) when the var is unset, empty, non-numeric,
 * `< 0`, or `> 1`. Fail-safe: this never throws — a bad value degrades to `1.0`
 * so a misconfigured env can never silently drop data.
 *
 * Only the high-frequency `call_started` / `call_completed` events are gated by
 * this rate (see `TelemetryClient`); activation and error events are always
 * delivered regardless. Mirrors `sample_rate` in `env.py`.
 */
export function sampleRate(): number {
  const raw = process.env.PATTER_TELEMETRY_SAMPLE;
  if (raw === undefined) return 1.0;
  const trimmed = raw.trim();
  if (trimmed === '') return 1.0;
  const rate = Number(trimmed);
  // NaN/Infinity or out of range → fail safe to 1.0 (keep everything). A value
  // of exactly 0 is honored (drop all sampleable events); only invalid input
  // degrades to the no-sampling default.
  if (!Number.isFinite(rate) || rate < 0 || rate > 1) return 1.0;
  return rate;
}

/** True when running under a recognised CI provider. */
export function isCi(): boolean {
  return CI_ENV_VARS.some((name) => isTruthy(process.env[name]));
}

/**
 * True when running under a recognised test runner. Honors both the
 * cross-ecosystem `NODE_ENV=test` and the Patter-specific `PATTER_ENV=test` so the
 * suppression knob behaves identically to the Python SDK.
 */
export function isTest(): boolean {
  if (TEST_ENV_VARS.some((name) => process.env[name] !== undefined)) return true;
  const node = (process.env.NODE_ENV ?? '').trim().toLowerCase();
  const patter = (process.env.PATTER_ENV ?? '').trim().toLowerCase();
  return node === 'test' || patter === 'test';
}
