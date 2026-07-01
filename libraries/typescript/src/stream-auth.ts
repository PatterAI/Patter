/**
 * Per-call media-stream authentication (GitHub issue #204).
 *
 * The media-stream WebSocket endpoints (`/ws/stream/{call_id}` and the Telnyx /
 * Plivo variants) are reachable by any peer that can hit the public host. The
 * HTTP carrier webhooks are signature-validated and fail closed, but the WS
 * upgrade previously had no authentication — an unauthenticated peer could send
 * a `start` frame and drive a full STT→LLM→TTS session on the operator's
 * provider keys (toll fraud) or converse to extract the agent's system prompt
 * and tool list.
 *
 * The fix mints a high-entropy, per-call token inside the SIGNATURE-VALIDATED
 * carrier webhook (inbound) or the operator-initiated outbound dial, stores it
 * in this short-TTL map keyed by the call id, and requires the media WS to
 * present it before any provider session opens. Because the token only ever
 * leaves via an authenticated channel, an unsigned attacker can never obtain a
 * valid one.
 *
 * Mirrors the Python `getpatter.stream_auth.StreamTokenStore`.
 */

import crypto from 'node:crypto';

/**
 * TTL for a minted per-call stream-auth token. The signed-webhook → media-WS
 * handshake completes in seconds; 120 s is a generous ceiling that still allows
 * a legitimate carrier reconnect within the window (the token is kept valid —
 * NOT single-use — so a carrier reconnect inside the TTL is not rejected).
 */
export const STREAM_TOKEN_TTL_MS = 120_000;

interface StreamTokenEntry {
  readonly token: string;
  /** Unix epoch milliseconds after which the entry is invalid. */
  readonly expiresAt: number;
}

/**
 * Generate a high-entropy, URL-safe stream-auth token.
 *
 * 32 random bytes, base64url-encoded (no padding) — parity with the Python
 * `secrets.token_urlsafe(32)`.
 */
export function generateStreamToken(): string {
  return crypto.randomBytes(32).toString('base64url');
}

/**
 * Constant-time string comparison. Returns `false` for unequal-length inputs
 * (the token length is fixed, so this leaks nothing an attacker can use) and
 * uses `crypto.timingSafeEqual` for the equal-length case. Mirrors Python's
 * `hmac.compare_digest`.
 */
export function constantTimeEquals(a: string, b: string): boolean {
  const bufA = Buffer.from(a, 'utf8');
  const bufB = Buffer.from(b, 'utf8');
  if (bufA.length !== bufB.length) return false;
  return crypto.timingSafeEqual(bufA, bufB);
}

/**
 * In-memory `{ callKey -> { token, expiresAt } }` store, one instance per
 * embedded server. Entries are minted by the signed webhook / outbound dial and
 * validated by the media-stream WS handler before the billable provider session
 * opens. Expired entries are pruned opportunistically on every operation, so
 * the map cannot grow unbounded even under a mint-heavy load.
 */
export class StreamTokenStore {
  private readonly entries = new Map<string, StreamTokenEntry>();

  constructor(private readonly ttlMs: number = STREAM_TOKEN_TTL_MS) {}

  /**
   * Mint a fresh token for `callKey`, store it, and return it. The stored
   * token replaces any previous one for the same key (a re-issued webhook wins).
   */
  mint(callKey: string, now: number = Date.now()): string {
    const token = generateStreamToken();
    this.register(callKey, token, now);
    return token;
  }

  /**
   * Store an already-generated token for `callKey`. Used by the outbound Twilio
   * path, where the token is embedded in inline TwiML before the carrier REST
   * response reveals the call id the WS will present.
   */
  register(callKey: string, token: string, now: number = Date.now()): void {
    this.prune(now);
    this.entries.set(callKey, { token, expiresAt: now + this.ttlMs });
  }

  /**
   * Return `true` iff `presented` matches the stored, unexpired token for
   * `callKey`. Constant-time compare; the entry is KEPT valid within the TTL so
   * a legitimate carrier reconnect inside the window is accepted. Missing,
   * expired, or mismatched tokens return `false`.
   */
  validate(callKey: string, presented: string | undefined | null, now: number = Date.now()): boolean {
    this.prune(now);
    const entry = this.entries.get(callKey);
    if (!entry) return false;
    if (entry.expiresAt <= now) {
      this.entries.delete(callKey);
      return false;
    }
    if (!presented) return false;
    return constantTimeEquals(presented, entry.token);
  }

  /** Drop every expired entry. Invoked opportunistically on mint/register/validate. */
  prune(now: number = Date.now()): void {
    for (const [key, entry] of this.entries) {
      if (entry.expiresAt <= now) this.entries.delete(key);
    }
  }

  /** Number of live (possibly-expired-but-not-yet-pruned) entries. Test/introspection only. */
  get size(): number {
    return this.entries.size;
  }
}
