/**
 * Public error taxonomy for the Patter SDK.
 *
 * Every Patter exception carries a stable, machine-readable {@link ErrorCode}
 * on its `code` property. Downstream code can branch on the code without
 * relying on class name strings or message parsing.
 *
 * The class hierarchy is preserved for backward compatibility — existing
 * `instanceof PatterConnectionError` checks keep working — and the enum is
 * purely additive.
 *
 * Mirrored byte-for-byte by the Python `ErrorCode` StrEnum in
 * `libraries/python/getpatter/exceptions.py`.
 */

/**
 * Stable, machine-readable error codes attached to every Patter exception.
 *
 * Values are short, `UPPER_SNAKE_CASE` strings. Existing values must never
 * change — downstream callers branch on them. New codes are additive.
 *
 * This is shipped as a `const` object plus value-union type rather than a
 * TS `enum` so it's tree-shakeable and compatible with `verbatimModuleSyntax`.
 */
export const ErrorCode = {
  /** Invalid constructor args, missing required env var, frozen-config violation. */
  CONFIG: "CONFIG",
  /** WebSocket connect failure, HTTP 5xx from provider, network error. */
  CONNECTION: "CONNECTION",
  /** Provider rejected our credentials (HTTP 401/403, invalid signature). */
  AUTH: "AUTH",
  /** Provider response, voicemail post, or other awaited operation timed out. */
  TIMEOUT: "TIMEOUT",
  /** Provider returned HTTP 429. */
  RATE_LIMIT: "RATE_LIMIT",
  /** Twilio / Telnyx webhook signature verification failed. */
  WEBHOOK_VERIFICATION: "WEBHOOK_VERIFICATION",
  /** Caller passed a malformed phone number, tool arg, etc. */
  INPUT_VALIDATION: "INPUT_VALIDATION",
  /** Generic catch-all for unexpected upstream provider failures. */
  PROVIDER_ERROR: "PROVIDER_ERROR",
  /** Phone number provisioning, webhook configuration, or carrier setup failed. */
  PROVISION: "PROVISION",
  /** Assertion failed / unexpected internal state. Likely a Patter bug. */
  INTERNAL: "INTERNAL",
} as const;

export type ErrorCode = (typeof ErrorCode)[keyof typeof ErrorCode];

export class PatterError extends Error {
  /** Stable, machine-readable error code. Subclasses set the default. */
  readonly code: ErrorCode;

  constructor(message: string, options?: { code?: ErrorCode }) {
    super(message);
    this.name = "PatterError";
    this.code = options?.code ?? ErrorCode.INTERNAL;
  }
}

export class PatterConnectionError extends PatterError {
  constructor(message: string, options?: { code?: ErrorCode }) {
    super(message, { code: options?.code ?? ErrorCode.CONNECTION });
    this.name = "PatterConnectionError";
  }
}

export class AuthenticationError extends PatterError {
  constructor(message: string, options?: { code?: ErrorCode }) {
    super(message, { code: options?.code ?? ErrorCode.AUTH });
    this.name = "AuthenticationError";
  }
}

export class ProvisionError extends PatterError {
  constructor(message: string, options?: { code?: ErrorCode }) {
    super(message, { code: options?.code ?? ErrorCode.PROVISION });
    this.name = "ProvisionError";
  }
}

/** Thrown when a provider returns HTTP 429 on connect/upgrade. */
export class RateLimitError extends PatterConnectionError {
  constructor(message: string, options?: { code?: ErrorCode }) {
    super(message, { code: options?.code ?? ErrorCode.RATE_LIMIT });
    this.name = "RateLimitError";
  }
}
