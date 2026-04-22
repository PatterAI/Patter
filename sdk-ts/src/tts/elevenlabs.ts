/** ElevenLabs TTS for Patter pipeline mode. */
import { ElevenLabsTTS as _ElevenLabsTTS } from "../providers/elevenlabs-tts";

export interface ElevenLabsTTSOptions {
  /** API key. Falls back to ELEVENLABS_API_KEY env var when omitted. */
  apiKey?: string;
  voiceId?: string;
  modelId?: string;
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
      opts.voiceId ?? "21m00Tcm4TlvDq8ikWAM",
      opts.modelId ?? "eleven_turbo_v2_5",
      opts.outputFormat ?? "pcm_16000",
    );
  }
}
