/** Twilio carrier credentials holder for Patter. */

/** Constructor options for the Twilio {@link Carrier}. */
export interface TwilioCarrierOptions {
  /** Twilio Account SID. Falls back to TWILIO_ACCOUNT_SID env var. */
  accountSid?: string;
  /** Twilio Auth Token. Falls back to TWILIO_AUTH_TOKEN env var. */
  authToken?: string;
}

/**
 * Twilio telephony carrier — holds Account SID + Auth Token.
 *
 * @example
 * ```ts
 * import * as twilio from "getpatter/telephony/twilio";
 * const carrier = new twilio.Carrier();                     // reads env
 * const carrier = new twilio.Carrier({ accountSid: "AC...", authToken: "..." });
 * ```
 */
export class Carrier {
  readonly kind = "twilio" as const;
  readonly accountSid: string;
  readonly authToken: string;

  constructor(opts: TwilioCarrierOptions = {}) {
    const sid = opts.accountSid ?? process.env.TWILIO_ACCOUNT_SID;
    const tok = opts.authToken ?? process.env.TWILIO_AUTH_TOKEN;
    if (!sid) {
      throw new Error(
        "Twilio carrier requires accountSid. Pass { accountSid: 'AC...' } or " +
          "set TWILIO_ACCOUNT_SID in the environment.",
      );
    }
    if (!tok) {
      throw new Error(
        "Twilio carrier requires authToken. Pass { authToken: '...' } or " +
          "set TWILIO_AUTH_TOKEN in the environment.",
      );
    }
    this.accountSid = sid;
    this.authToken = tok;
  }
}
