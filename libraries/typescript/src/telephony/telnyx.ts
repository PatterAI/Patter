/** Telnyx carrier credentials holder for Patter. */

export interface TelnyxCarrierOptions {
  /** Telnyx API key. Falls back to TELNYX_API_KEY env var. */
  apiKey?: string;
  /** Telnyx connection ID. Falls back to TELNYX_CONNECTION_ID env var. */
  connectionId?: string;
  /** Optional Ed25519 public key for webhook signature verification. Falls back to TELNYX_PUBLIC_KEY env var. */
  publicKey?: string;
}

/**
 * Telnyx telephony carrier — holds API key, connection ID, and optional webhook public key.
 *
 * @example
 * ```ts
 * import * as telnyx from "getpatter/telephony/telnyx";
 * const carrier = new telnyx.Carrier();                     // reads env
 * const carrier = new telnyx.Carrier({ apiKey: "KEY...", connectionId: "123" });
 * ```
 */
export class Carrier {
  readonly kind = "telnyx" as const;
  readonly apiKey: string;
  readonly connectionId: string;
  readonly publicKey: string | undefined;

  constructor(opts: TelnyxCarrierOptions = {}) {
    const key = opts.apiKey ?? process.env.TELNYX_API_KEY;
    const conn = opts.connectionId ?? process.env.TELNYX_CONNECTION_ID;
    const pub = opts.publicKey ?? process.env.TELNYX_PUBLIC_KEY;
    if (!key) {
      throw new Error(
        "Telnyx carrier requires apiKey. Pass { apiKey: '...' } or " +
          "set TELNYX_API_KEY in the environment.",
      );
    }
    if (!conn) {
      throw new Error(
        "Telnyx carrier requires connectionId. Pass { connectionId: '...' } or " +
          "set TELNYX_CONNECTION_ID in the environment.",
      );
    }
    this.apiKey = key;
    this.connectionId = conn;
    this.publicKey = pub;
  }
}
