/** Speechmatics streaming STT for Patter pipeline mode. */
import {
  SpeechmaticsSTT as _SpeechmaticsSTT,
  type SpeechmaticsSTTOptions as _SpeechmaticsSTTOptions,
} from "../providers/speechmatics-stt";

// Re-export the canonical options type from the provider so callers have a
// single source of truth and can import either path.
export type SpeechmaticsSTTOptions = _SpeechmaticsSTTOptions & {
  /** API key. Falls back to SPEECHMATICS_API_KEY env var when omitted. */
  apiKey?: string;
};

/**
 * Speechmatics streaming STT.
 *
 * @example
 * ```ts
 * import * as speechmatics from "getpatter/stt/speechmatics";
 * const stt = new speechmatics.STT();              // reads SPEECHMATICS_API_KEY
 * const stt = new speechmatics.STT({ apiKey: "sm_...", language: "en" });
 * ```
 */
export class STT extends _SpeechmaticsSTT {
  static readonly providerKey = "speechmatics";
  constructor(opts: SpeechmaticsSTTOptions = {}) {
    const key = opts.apiKey ?? process.env.SPEECHMATICS_API_KEY;
    if (!key) {
      throw new Error(
        "Speechmatics STT requires an apiKey. Pass { apiKey: 'sm_...' } or " +
          "set SPEECHMATICS_API_KEY in the environment.",
      );
    }
    super(key, opts);
  }
}
