/** Deepgram streaming STT for Patter pipeline mode. */
import {
  DeepgramSTT as _DeepgramSTT,
  type DeepgramSTTOptions as _DeepgramSTTOptions,
} from "../providers/deepgram-stt";

// Re-export the canonical options type from the provider so callers have a
// single source of truth and can import either path (BUG #13 follow-up).
export type DeepgramSTTOptions = _DeepgramSTTOptions & {
  /** API key. Falls back to DEEPGRAM_API_KEY env var when omitted. */
  apiKey?: string;
  language?: string;
};

/**
 * Deepgram streaming STT.
 *
 * @example
 * ```ts
 * import * as deepgram from "getpatter/stt/deepgram";
 * const stt = new deepgram.STT();              // reads DEEPGRAM_API_KEY
 * const stt = new deepgram.STT({ apiKey: "dg_...", endpointingMs: 80 });
 * ```
 */
export class STT extends _DeepgramSTT {
  static readonly providerKey = "deepgram";
  constructor(opts: DeepgramSTTOptions = {}) {
    const key = opts.apiKey ?? process.env.DEEPGRAM_API_KEY;
    if (!key) {
      throw new Error(
        "Deepgram STT requires an apiKey. Pass { apiKey: 'dg_...' } or " +
          "set DEEPGRAM_API_KEY in the environment.",
      );
    }
    super(
      key,
      opts.language ?? "en",
      opts.model ?? "nova-3",
      opts.encoding ?? "linear16",
      opts.sampleRate ?? 16000,
      {
        endpointingMs: opts.endpointingMs ?? 150,
        utteranceEndMs: opts.utteranceEndMs === null ? null : opts.utteranceEndMs ?? 1000,
        smartFormat: opts.smartFormat ?? true,
        interimResults: opts.interimResults ?? true,
        ...(opts.vadEvents !== undefined ? { vadEvents: opts.vadEvents } : {}),
      },
    );
  }
}
