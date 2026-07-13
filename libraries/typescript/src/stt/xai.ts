/** xAI (Grok) streaming STT for Patter pipeline mode. */
import { XAISTT as _XAISTT, type XAISTTOptions as _ProviderOptions } from '../providers/xai-stt';

/** Constructor options for the xAI `STT` adapter. */
export interface XAISTTOptions extends _ProviderOptions {
  /** API key. Falls back to XAI_API_KEY env var when omitted. */
  apiKey?: string;
}

/**
 * xAI (Grok) streaming STT (`wss://api.x.ai/v1/stt`).
 *
 * @example
 * ```ts
 * import * as xai from "getpatter/stt/xai";
 * const stt = new xai.STT();                     // reads XAI_API_KEY
 * const stt = new xai.STT({ apiKey: "xai-...", keyterms: ["Patter"] });
 * ```
 */
export class STT extends _XAISTT {
  static readonly providerKey = 'xai_stt';
  constructor(opts: XAISTTOptions = {}) {
    const key = opts.apiKey ?? process.env.XAI_API_KEY;
    if (!key) {
      throw new Error(
        "xAI STT requires an apiKey. Pass { apiKey: 'xai-...' } or " +
          'set XAI_API_KEY in the environment.',
      );
    }
    const { apiKey: _ignored, ...rest } = opts;
    void _ignored;
    super(key, rest);
  }
}
