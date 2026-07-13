/** xAI streaming STT for Patter pipeline mode. */
import { XaiSTT as _XaiSTT, type XaiSTTEncoding } from "../providers/xai-stt";

/** Constructor options for the xAI `STT` adapter. */
export interface XaiSTTOptions {
  /** API key. Falls back to XAI_API_KEY env var when omitted. */
  apiKey?: string;
  encoding?: XaiSTTEncoding | string;
  sampleRate?: number;
  interimResults?: boolean;
  language?: string;
  endpointingMs?: number;
  smartTurn?: number;
  smartTurnTimeoutMs?: number;
  keyterms?: readonly string[];
  diarize?: boolean;
  fillerWords?: boolean;
  baseUrl?: string;
}

/**
 * xAI streaming STT (Beta).
 *
 * @example
 * ```ts
 * import * as xai from "getpatter/stt/xai";
 * const stt = new xai.STT();              // reads XAI_API_KEY
 * const stt = new xai.STT({ apiKey: "..." });
 * ```
 */
export class STT extends _XaiSTT {
  static readonly providerKey = "xai";
  constructor(opts: XaiSTTOptions = {}) {
    const key = opts.apiKey ?? process.env.XAI_API_KEY;
    if (!key) {
      throw new Error(
        "xAI STT requires an apiKey. Pass { apiKey: '...' } or " +
          "set XAI_API_KEY in the environment.",
      );
    }
    const { apiKey: _ignored, ...rest } = opts;
    void _ignored;
    super(key, rest);
  }
}
