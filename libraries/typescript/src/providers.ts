import type { STTConfig, TTSConfig } from "./types";

/**
 * Config envelope for realtime / ConvAI pipelines — mirrors the wire-level
 * shape consumed by the backend. Kept narrow on purpose so callers can pass a
 * plain object literal if they prefer.
 */
export interface RealtimeConfig {
  readonly provider: string;
  readonly apiKey: string;
  readonly model?: string;
  readonly voice?: string;
  readonly options?: Record<string, unknown>;
}

class STTConfigImpl implements STTConfig {
  readonly provider: string;
  readonly apiKey: string;
  readonly language: string;
  readonly options?: Record<string, unknown>;

  constructor(
    provider: string,
    apiKey: string,
    language: string = "en",
    options?: Record<string, unknown>,
  ) {
    this.provider = provider;
    this.apiKey = apiKey;
    this.language = language;
    if (options) this.options = options;
  }

  toDict(): Record<string, string | Record<string, unknown>> {
    const out: Record<string, string | Record<string, unknown>> = {
      provider: this.provider,
      api_key: this.apiKey,
      language: this.language,
    };
    if (this.options) out.options = { ...this.options };
    return out;
  }
}

class TTSConfigImpl implements TTSConfig {
  readonly provider: string;
  readonly apiKey: string;
  readonly voice: string;

  constructor(provider: string, apiKey: string, voice: string = "alloy") {
    this.provider = provider;
    this.apiKey = apiKey;
    this.voice = voice;
  }

  toDict(): Record<string, string> {
    return { provider: this.provider, api_key: this.apiKey, voice: this.voice };
  }
}

/**
 * Deepgram STT config builder. Tune latency via ``endpointingMs`` /
 * ``utteranceEndMs``. Internal only — public code should use ``DeepgramSTT``
 * from ``getpatter/stt/deepgram``.
 */
export function deepgram(opts: {
  apiKey: string;
  language?: string;
  model?: string;
  endpointingMs?: number;
  utteranceEndMs?: number | null;
  smartFormat?: boolean;
  interimResults?: boolean;
  vadEvents?: boolean;
}): STTConfig {
  const options: Record<string, unknown> = {
    model: opts.model ?? "nova-3",
    endpointing_ms: opts.endpointingMs ?? 150,
    utterance_end_ms: opts.utteranceEndMs === null ? null : (opts.utteranceEndMs ?? 1000),
    smart_format: opts.smartFormat ?? true,
    interim_results: opts.interimResults ?? true,
  };
  if (opts.vadEvents !== undefined) options.vad_events = opts.vadEvents;
  return new STTConfigImpl("deepgram", opts.apiKey, opts.language ?? "en", options);
}

export function whisper(opts: { apiKey: string; language?: string }): STTConfig {
  return new STTConfigImpl("whisper", opts.apiKey, opts.language ?? "en");
}

export function elevenlabs(opts: { apiKey: string; voice?: string }): TTSConfig {
  return new TTSConfigImpl("elevenlabs", opts.apiKey, opts.voice ?? "rachel");
}

export function openaiTts(opts: { apiKey: string; voice?: string }): TTSConfig {
  return new TTSConfigImpl("openai", opts.apiKey, opts.voice ?? "alloy");
}

// ---------------------------------------------------------------------------
// Additional STT helpers (parity with Python getpatter.providers)
// ---------------------------------------------------------------------------

/** Soniox real-time STT config helper. */
export function soniox(opts: { apiKey: string; language?: string }): STTConfig {
  return new STTConfigImpl("soniox", opts.apiKey, opts.language ?? "en");
}

/**
 * Speechmatics STT config helper.
 *
 * NOTE: the Speechmatics adapter is currently Python-only. Calling this helper
 * throws a clear error so callers can switch providers or use the Python SDK
 * until the TS adapter ships.
 */
export function speechmatics(_opts: { apiKey: string; language?: string }): STTConfig {
  throw new Error(
    "speechmatics() is Python-only right now — the TS Speechmatics adapter " +
      "has not shipped yet. Use the Python SDK (sdk-py) or pick another STT " +
      "provider such as deepgram() / assemblyai() / soniox().",
  );
}

/** AssemblyAI real-time STT config helper. */
export function assemblyai(opts: { apiKey: string; language?: string }): STTConfig {
  return new STTConfigImpl("assemblyai", opts.apiKey, opts.language ?? "en");
}

// ---------------------------------------------------------------------------
// Additional TTS helpers
// ---------------------------------------------------------------------------

/** Cartesia TTS config helper. Default voice matches Python SDK. */
export function cartesia(opts: { apiKey: string; voice?: string }): TTSConfig {
  return new TTSConfigImpl(
    "cartesia",
    opts.apiKey,
    opts.voice ?? "f786b574-daa5-4673-aa0c-cbe3e8534c02",
  );
}

/** Rime TTS config helper. */
export function rime(opts: { apiKey: string; voice?: string }): TTSConfig {
  return new TTSConfigImpl("rime", opts.apiKey, opts.voice ?? "astra");
}

/** LMNT TTS config helper. */
export function lmnt(opts: { apiKey: string; voice?: string }): TTSConfig {
  return new TTSConfigImpl("lmnt", opts.apiKey, opts.voice ?? "leah");
}

// ---------------------------------------------------------------------------
// Realtime / ConvAI helpers
// ---------------------------------------------------------------------------

/**
 * Ultravox realtime engine config helper.
 *
 * Returns a ``RealtimeConfig`` envelope that the backend can dispatch. For
 * programmatic control over a live session use ``UltravoxRealtimeAdapter``
 * directly.
 */
export function ultravox(opts: {
  apiKey: string;
  model?: string;
  voice?: string;
}): RealtimeConfig {
  return {
    provider: "ultravox",
    apiKey: opts.apiKey,
    model: opts.model,
    voice: opts.voice,
  };
}

/**
 * Google Gemini Live realtime engine config helper. See
 * ``GeminiLiveAdapter`` for direct session control.
 */
export function geminiLive(opts: {
  apiKey: string;
  model?: string;
  voice?: string;
}): RealtimeConfig {
  return {
    provider: "gemini_live",
    apiKey: opts.apiKey,
    model: opts.model,
    voice: opts.voice,
  };
}
