/** Soniox streaming STT for Patter pipeline mode. */
import { SonioxSTT as _SonioxSTT } from "../providers/soniox-stt";

export interface SonioxSTTOptions {
  /** API key. Falls back to SONIOX_API_KEY env var when omitted. */
  apiKey?: string;
  model?: string;
  languageHints?: string[];
  languageHintsStrict?: boolean;
  sampleRate?: number;
  numChannels?: number;
  enableSpeakerDiarization?: boolean;
  enableLanguageIdentification?: boolean;
  maxEndpointDelayMs?: number;
  clientReferenceId?: string;
  baseUrl?: string;
}

/**
 * Soniox streaming STT.
 *
 * @example
 * ```ts
 * import * as soniox from "getpatter/stt/soniox";
 * const stt = new soniox.STT();              // reads SONIOX_API_KEY
 * const stt = new soniox.STT({ apiKey: "..." });
 * ```
 */
export class STT extends _SonioxSTT {
  static readonly providerKey = "soniox";
  constructor(opts: SonioxSTTOptions = {}) {
    const key = opts.apiKey ?? process.env.SONIOX_API_KEY;
    if (!key) {
      throw new Error(
        "Soniox STT requires an apiKey. Pass { apiKey: '...' } or " +
          "set SONIOX_API_KEY in the environment.",
      );
    }
    const { apiKey: _ignored, ...rest } = opts;
    void _ignored;
    super(key, rest);
  }
}
