/** ElevenLabs TTS for Patter pipeline mode. */
import {
  ElevenLabsWebSocketTTS as _ElevenLabsWebSocketTTS,
  type ElevenLabsWebSocketTTSOptions,
} from "../providers/elevenlabs-ws-tts";
import type { ElevenLabsModel } from "../providers/elevenlabs-tts";

export type { ElevenLabsModel };

/** Constructor options for the ElevenLabs `TTS` adapter. */
export interface ElevenLabsTTSOptions {
  /** API key. Falls back to ELEVENLABS_API_KEY env var when omitted. */
  readonly apiKey?: string;
  readonly voiceId?: string;
  /**
   * ElevenLabs voice model ID. Default is ``eleven_flash_v2_5`` (lowest TTFT).
   * Pass ``eleven_v3`` for highest quality, or any string for forward-compat.
   *
   * Note: ``eleven_v3`` is HTTP-only — the default WebSocket transport
   * rejects it. Use ``ElevenLabsRestTTS`` for ``eleven_v3``.
   */
  readonly modelId?: ElevenLabsModel | string;
  readonly outputFormat?: string;
  /**
   * BCP-47 language code (e.g. `"it"`, `"es"`). Forwarded to ElevenLabs as
   * the `language_code` request body field — required for multilingual /
   * Flash v2.5 voices to render the right accent.
   */
  readonly languageCode?: string;
  /** ElevenLabs `voice_settings` object (stability, similarity_boost, …). */
  readonly voiceSettings?: Record<string, unknown>;
}

/** Options for the carrier-specific factories — same as the constructor minus `outputFormat`. */
export type ElevenLabsCarrierOptions = Omit<ElevenLabsTTSOptions, "outputFormat">;

function resolveApiKey(apiKey: string | undefined): string {
  const key = apiKey ?? process.env.ELEVENLABS_API_KEY;
  if (!key) {
    throw new Error(
      "ElevenLabs TTS requires an apiKey. Pass { apiKey: '...' } or " +
        "set ELEVENLABS_API_KEY in the environment.",
    );
  }
  return key;
}

function buildWsOpts(opts: ElevenLabsTTSOptions): ElevenLabsWebSocketTTSOptions {
  const out: ElevenLabsWebSocketTTSOptions = {
    apiKey: resolveApiKey(opts.apiKey),
    // Preserve the REST-era default voice (Sarah / EXAVITQu4vr4xnSDxMaL) so
    // the WS flip is transparent for callers that relied on it. The raw WS
    // provider has its own default (21m00Tcm4TlvDq8ikWAM) which we override
    // here for backward-compat.
    voiceId: opts.voiceId ?? "EXAVITQu4vr4xnSDxMaL",
    modelId: opts.modelId ?? "eleven_flash_v2_5",
    outputFormat: opts.outputFormat ?? "pcm_16000",
    autoMode: true,
  };
  if (opts.voiceSettings !== undefined) out.voiceSettings = opts.voiceSettings;
  if (opts.languageCode !== undefined) out.languageCode = opts.languageCode;
  return out;
}

/**
 * ElevenLabs TTS.
 *
 * Default = WebSocket streaming (added 0.6.1). For HTTP REST opt-out:
 * use `new ElevenLabsRestTTS(...)` directly.
 *
 * @example
 * ```ts
 * import * as elevenlabs from "getpatter/tts/elevenlabs";
 * const tts = new elevenlabs.TTS();              // reads ELEVENLABS_API_KEY
 * const tts = new elevenlabs.TTS({ apiKey: "...", voiceId: "rachel" });
 * ```
 *
 * **Telephony optimization** — use {@link TTS.forTwilio} (μ-law @ 8 kHz,
 * native Twilio Media Streams format) or {@link TTS.forTelnyx} (PCM @
 * 16 kHz, native Telnyx default) on phone calls to skip the SDK-side
 * resampling / transcoding step.
 */
export class TTS extends _ElevenLabsWebSocketTTS {
  static readonly providerKey = "elevenlabs_ws";
  constructor(opts: ElevenLabsTTSOptions = {}) {
    super(buildWsOpts(opts));
  }

  /** Pipeline TTS pre-configured for Twilio Media Streams (`ulaw_8000`). */
  static override forTwilio(opts?: ElevenLabsCarrierOptions): TTS;
  // Parent-compatible overload — accepts the legacy positional form too.
  static override forTwilio(
    apiKey: string,
    options?: Omit<ElevenLabsTTSOptions, "outputFormat">,
  ): TTS;
  static override forTwilio(
    arg1?: string | ElevenLabsCarrierOptions,
    arg2?: Omit<ElevenLabsTTSOptions, "outputFormat">,
  ): TTS {
    const opts: ElevenLabsCarrierOptions =
      typeof arg1 === "string" ? { apiKey: arg1, ...(arg2 ?? {}) } : (arg1 ?? {});
    return new TTS({ ...opts, outputFormat: "ulaw_8000" });
  }

  /** Pipeline TTS pre-configured for Telnyx (`pcm_16000`). */
  static override forTelnyx(opts?: ElevenLabsCarrierOptions): TTS;
  static override forTelnyx(
    apiKey: string,
    options?: Omit<ElevenLabsTTSOptions, "outputFormat">,
  ): TTS;
  static override forTelnyx(
    arg1?: string | ElevenLabsCarrierOptions,
    arg2?: Omit<ElevenLabsTTSOptions, "outputFormat">,
  ): TTS {
    const opts: ElevenLabsCarrierOptions =
      typeof arg1 === "string" ? { apiKey: arg1, ...(arg2 ?? {}) } : (arg1 ?? {});
    return new TTS({ ...opts, outputFormat: "pcm_16000" });
  }
}
