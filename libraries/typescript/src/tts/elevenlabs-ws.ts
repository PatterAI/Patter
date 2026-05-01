/** ElevenLabs WebSocket TTS for Patter pipeline mode (opt-in low-latency). */
import {
  ElevenLabsWebSocketTTS as _ElevenLabsWebSocketTTS,
  type ElevenLabsWebSocketTTSOptions,
} from '../providers/elevenlabs-ws-tts';
import type { ElevenLabsModel } from '../providers/elevenlabs-tts';

export type { ElevenLabsModel };

/** Constructor options for the ElevenLabs WebSocket `TTS` adapter. */
export interface ElevenLabsWebSocketOptions {
  /** API key. Falls back to ELEVENLABS_API_KEY env var when omitted. */
  apiKey?: string;
  voiceId?: string;
  modelId?: ElevenLabsModel | string;
  outputFormat?: string;
  /** Let the server pick chunk timing. Default true. */
  autoMode?: boolean;
  voiceSettings?: Record<string, unknown>;
  languageCode?: string;
  /** WS keep-alive timeout in seconds (5–180). Default 60. */
  inactivityTimeout?: number;
  /** Manual chunk schedule, only used when ``autoMode: false``. */
  chunkLengthSchedule?: number[];
}

/** Options for the carrier-specific factories — same as the constructor minus `outputFormat`. */
export type ElevenLabsWebSocketCarrierOptions = Omit<ElevenLabsWebSocketOptions, 'outputFormat'>;

function resolveApiKey(apiKey: string | undefined): string {
  const key = apiKey ?? process.env.ELEVENLABS_API_KEY;
  if (!key) {
    throw new Error(
      "ElevenLabs WebSocket TTS requires an apiKey. Pass { apiKey: '...' } or " +
        'set ELEVENLABS_API_KEY in the environment.',
    );
  }
  return key;
}

function buildOpts(opts: ElevenLabsWebSocketOptions): ElevenLabsWebSocketTTSOptions {
  // Voice ID default is owned by the provider class — passing ``undefined``
  // here lets it apply its own default (parity Python ↔ TS).
  const out: ElevenLabsWebSocketTTSOptions = {
    apiKey: resolveApiKey(opts.apiKey),
    modelId: opts.modelId ?? 'eleven_flash_v2_5',
    outputFormat: opts.outputFormat ?? 'pcm_16000',
    autoMode: opts.autoMode ?? true,
  };
  if (opts.voiceId !== undefined) out.voiceId = opts.voiceId;
  if (opts.voiceSettings !== undefined) out.voiceSettings = opts.voiceSettings;
  if (opts.languageCode !== undefined) out.languageCode = opts.languageCode;
  if (opts.inactivityTimeout !== undefined) out.inactivityTimeout = opts.inactivityTimeout;
  if (opts.chunkLengthSchedule !== undefined) out.chunkLengthSchedule = opts.chunkLengthSchedule;
  return out;
}

/**
 * ElevenLabs streaming TTS over WebSocket.
 *
 * Drop-in replacement for `getpatter/tts/elevenlabs.TTS` (HTTP) using the
 * `stream-input` WebSocket endpoint. Saves the per-utterance HTTP request
 * setup time; otherwise behaves identically.
 *
 * @example
 * ```ts
 * import * as elevenlabsWs from "getpatter/tts/elevenlabs-ws";
 * const tts = new elevenlabsWs.TTS();              // reads ELEVENLABS_API_KEY
 * const tts = elevenlabsWs.TTS.forTwilio({ apiKey: "..." });
 * ```
 *
 * **Telephony optimisation** — use {@link TTS.forTwilio} (μ-law @ 8 kHz)
 * or {@link TTS.forTelnyx} (PCM @ 16 kHz) on phone calls.
 */
export class TTS extends _ElevenLabsWebSocketTTS {
  static readonly providerKey = 'elevenlabs_ws';

  constructor(opts: ElevenLabsWebSocketOptions = {}) {
    super(buildOpts(opts));
  }

  /** WebSocket TTS pre-configured for Twilio Media Streams (`ulaw_8000`). */
  static override forTwilio(opts: ElevenLabsWebSocketCarrierOptions = {}): TTS {
    return new TTS({ ...opts, outputFormat: 'ulaw_8000' });
  }

  /** WebSocket TTS pre-configured for Telnyx (`pcm_16000`). */
  static override forTelnyx(opts: ElevenLabsWebSocketCarrierOptions = {}): TTS {
    return new TTS({ ...opts, outputFormat: 'pcm_16000' });
  }
}
