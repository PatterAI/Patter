import type { STTConfig, TTSConfig } from "./types";
import { XAI_DEFAULT_VOICE, normalizeXaiVoice } from "./providers/xai-voices";

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

/** Speechmatics real-time STT config helper. */
export function speechmatics(opts: { apiKey: string; language?: string }): STTConfig {
  return new STTConfigImpl("speechmatics", opts.apiKey, opts.language ?? "en");
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

/**
 * Inworld TTS config helper (parity with Python ``getpatter.providers.inworld``).
 * For pipeline use the documented path is the direct adapter
 * ``new InworldTTS({...})`` from ``getpatter``.
 */
export function inworld(opts: { apiKey: string; voice?: string }): TTSConfig {
  return new TTSConfigImpl("inworld", opts.apiKey, opts.voice ?? "Ashley");
}

/**
 * Soniox TTS config helper (parity with Python ``getpatter.providers.soniox_tts``).
 * Shares the ``SONIOX_API_KEY`` credential family with Soniox STT. For pipeline
 * use the documented path is the direct adapter ``new SonioxTTS({...})`` from
 * ``getpatter``.
 */
export function sonioxTts(opts: { apiKey: string; voice?: string }): TTSConfig {
  return new TTSConfigImpl("soniox_tts", opts.apiKey, opts.voice ?? "Adrian");
}

/**
 * Sarvam AI TTS config helper for Indian languages (parity with Python
 * ``getpatter.providers.sarvam``). ``voice`` is the Sarvam speaker id (default
 * ``shubh``). For pipeline use the documented path is the direct adapter
 * ``new SarvamTTS({...})`` from ``getpatter``.
 */
export function sarvam(opts: { apiKey: string; voice?: string }): TTSConfig {
  return new TTSConfigImpl("sarvam", opts.apiKey, opts.voice ?? "shubh");
}

/**
 * Fish Audio TTS config helper (parity with Python ``getpatter.providers.fish_audio``).
 *
 * ``voice`` is a Fish ``reference_id`` — a voice-model id from the Fish voice
 * library or one you cloned. Leave it empty to use the model's built-in voice.
 * For pipeline use the documented path is the direct adapter
 * ``new FishAudioTTS({...})`` from ``getpatter``.
 */
export function fishAudio(opts: { apiKey: string; voice?: string }): TTSConfig {
  return new TTSConfigImpl("fish_audio", opts.apiKey, opts.voice ?? "");
}

/**
 * Fish Audio ASR config helper (parity with Python
 * ``getpatter.providers.fish_audio_asr``). Fish transcription is a batch
 * endpoint, so the adapter uploads short windows instead of streaming — see
 * ``FishAudioSTT`` for the latency trade-off against Deepgram / Soniox.
 */
export function fishAudioAsr(opts: { apiKey: string; language?: string }): STTConfig {
  return new STTConfigImpl("fish_audio", opts.apiKey, opts.language ?? "en");
}

/**
 * xAI (Grok) TTS config helper (parity with Python ``getpatter.providers.xai``).
 *
 * ``voice`` is a built-in xAI voice id (see ``XAI_VOICES`` / ``isXaiBuiltinVoice``
 * from ``getpatter``) or a custom cloned ``voice_id``; built-in ids are
 * case-insensitive and normalized here, defaulting to ``"eve"``. For pipeline
 * use the documented path is the direct adapter ``new XaiTTS({...})`` from
 * ``getpatter``. Provider key ``"xai_tts"`` matches ``XaiTTS.providerKey`` and
 * the ``DEFAULT_PRICING`` entry it bills against.
 */
export function xai(opts: { apiKey: string; voice?: string }): TTSConfig {
  return new TTSConfigImpl(
    "xai_tts",
    opts.apiKey,
    normalizeXaiVoice(opts.voice ?? XAI_DEFAULT_VOICE),
  );
}

/**
 * xAI (Grok) streaming STT config helper (parity with Python
 * ``getpatter.providers.xai_asr``). Shares the ``XAI_API_KEY`` credential
 * family with xAI TTS and Realtime. Provider key ``"xai"`` matches
 * ``XaiSTT.providerKey`` and the ``DEFAULT_PRICING`` entry it bills against.
 */
export function xaiAsr(opts: { apiKey: string; language?: string }): STTConfig {
  return new STTConfigImpl("xai", opts.apiKey, opts.language ?? "en");
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
