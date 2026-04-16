import type { STTConfig, TTSConfig } from "./types";

class STTConfigImpl implements STTConfig {
  readonly provider: string;
  readonly apiKey: string;
  readonly language: string;

  constructor(provider: string, apiKey: string, language: string = "en") {
    this.provider = provider;
    this.apiKey = apiKey;
    this.language = language;
  }

  toDict(): Record<string, string> {
    return { provider: this.provider, api_key: this.apiKey, language: this.language };
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

export function deepgram(opts: { apiKey: string; language?: string }): STTConfig {
  return new STTConfigImpl("deepgram", opts.apiKey, opts.language ?? "en");
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

export function cartesia(opts: { apiKey: string; voice?: string }): TTSConfig {
  return new TTSConfigImpl(
    "cartesia",
    opts.apiKey,
    opts.voice ?? "f786b574-daa5-4673-aa0c-cbe3e8534c02",
  );
}

export function rime(opts: { apiKey: string; voice?: string }): TTSConfig {
  return new TTSConfigImpl("rime", opts.apiKey, opts.voice ?? "astra");
}

export function lmnt(opts: { apiKey: string; voice?: string }): TTSConfig {
  return new TTSConfigImpl("lmnt", opts.apiKey, opts.voice ?? "leah");
}
