/** xAI (Grok) TTS for Patter pipeline mode. */
import {
  XAITTS as _XAITTS,
  XAITTSCodec,
  XAITTSSampleRate,
  type XAITTSOptions as _ProviderOptions,
} from '../providers/xai-tts';

/** Constructor options for the xAI `TTS` adapter. */
export interface XAITTSOptions extends _ProviderOptions {
  /** API key. Falls back to XAI_API_KEY env var when omitted. */
  apiKey?: string;
}

/** Options for the carrier-specific factories — same minus the pinned format. */
export type XAICarrierOptions = Omit<XAITTSOptions, 'codec' | 'sampleRate'>;

function resolveApiKey(apiKey: string | undefined): string {
  const key = apiKey ?? process.env.XAI_API_KEY;
  if (!key) {
    throw new Error(
      "xAI TTS requires an apiKey. Pass { apiKey: 'xai-...' } or " +
        'set XAI_API_KEY in the environment.',
    );
  }
  return key;
}

/**
 * xAI (Grok) TTS — WebSocket streaming by default, `eve` voice.
 *
 * @example
 * ```ts
 * import * as xai from "getpatter/tts/xai";
 * const tts = new xai.TTS();                              // reads XAI_API_KEY
 * const tts = new xai.TTS({ apiKey: "xai-...", voice: "ara", language: "auto" });
 * ```
 *
 * **Telephony** — use {@link TTS.forTwilio} or {@link TTS.forTelnyx} on phone
 * calls. Both emit G.711 μ-law @ 8 kHz natively, so the pipeline skips
 * resampling and PCM → μ-law encoding entirely (bit-clean passthrough).
 */
export class TTS extends _XAITTS {
  static readonly providerKey = 'xai_tts';
  constructor(opts: XAITTSOptions = {}) {
    const key = resolveApiKey(opts.apiKey);
    const { apiKey: _ignored, ...rest } = opts;
    void _ignored;
    super(key, rest);
  }

  /** Pipeline TTS pre-configured for Twilio Media Streams (μ-law @ 8 kHz native). */
  static override forTwilio(opts?: XAICarrierOptions): TTS;
  // Parent-compatible overload — accepts the positional form too.
  static override forTwilio(apiKey: string, options?: XAICarrierOptions): TTS;
  static override forTwilio(
    arg1?: string | XAICarrierOptions,
    arg2?: XAICarrierOptions,
  ): TTS {
    const opts: XAICarrierOptions =
      typeof arg1 === 'string' ? { apiKey: arg1, ...(arg2 ?? {}) } : (arg1 ?? {});
    return new TTS({
      ...opts,
      codec: XAITTSCodec.MULAW,
      sampleRate: XAITTSSampleRate.HZ_8000,
    });
  }

  /** Pipeline TTS pre-configured for Telnyx (PCMU/μ-law @ 8 kHz wire passthrough). */
  static override forTelnyx(opts?: XAICarrierOptions): TTS;
  static override forTelnyx(apiKey: string, options?: XAICarrierOptions): TTS;
  static override forTelnyx(
    arg1?: string | XAICarrierOptions,
    arg2?: XAICarrierOptions,
  ): TTS {
    const opts: XAICarrierOptions =
      typeof arg1 === 'string' ? { apiKey: arg1, ...(arg2 ?? {}) } : (arg1 ?? {});
    return new TTS({
      ...opts,
      codec: XAITTSCodec.MULAW,
      sampleRate: XAITTSSampleRate.HZ_8000,
    });
  }
}
