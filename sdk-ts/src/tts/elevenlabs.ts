/** ElevenLabs TTS for Patter pipeline mode. */
import { ElevenLabsTTS as _ElevenLabsTTS, type ElevenLabsModel } from "../providers/elevenlabs-tts";

export type { ElevenLabsModel };

export interface ElevenLabsTTSOptions {
  /** API key. Falls back to ELEVENLABS_API_KEY env var when omitted. */
  apiKey?: string;
  voiceId?: string;
  /**
   * ElevenLabs voice model ID. Default is ``eleven_flash_v2_5`` (lowest TTFT).
   * Pass ``eleven_v3`` for highest quality, or any string for forward-compat.
   */
  modelId?: ElevenLabsModel | string;
  outputFormat?: string;
}

/**
 * ElevenLabs TTS.
 *
 * @example
 * ```ts
 * import * as elevenlabs from "getpatter/tts/elevenlabs";
 * const tts = new elevenlabs.TTS();              // reads ELEVENLABS_API_KEY
 * const tts = new elevenlabs.TTS({ apiKey: "...", voiceId: "rachel" });
 * ```
 */
export class TTS extends _ElevenLabsTTS {
  static readonly providerKey = "elevenlabs";
  constructor(opts: ElevenLabsTTSOptions = {}) {
    const key = opts.apiKey ?? process.env.ELEVENLABS_API_KEY;
    if (!key) {
      throw new Error(
        "ElevenLabs TTS requires an apiKey. Pass { apiKey: '...' } or " +
          "set ELEVENLABS_API_KEY in the environment.",
      );
    }
    super(
      key,
      opts.voiceId ?? "EXAVITQu4vr4xnSDxMaL",
      opts.modelId ?? "eleven_flash_v2_5",
      opts.outputFormat ?? "pcm_16000",
    );
  }
}
