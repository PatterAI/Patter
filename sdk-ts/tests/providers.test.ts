import { describe, it, expect } from "vitest";
import { deepgram, whisper, elevenlabs, openaiTts } from "../src/providers";
import { DeepgramSTT } from "../src/providers/deepgram-stt";
import { ElevenLabsTTS } from "../src/providers/elevenlabs-tts";

describe("deepgram", () => {
  it("returns STTConfig with correct provider", () => {
    const config = deepgram({ apiKey: "dg_test" });
    expect(config.provider).toBe("deepgram");
    expect(config.apiKey).toBe("dg_test");
    expect(config.language).toBe("en");
  });

  it("accepts custom language", () => {
    const config = deepgram({ apiKey: "dg_test", language: "it" });
    expect(config.language).toBe("it");
  });

  it("toDict returns snake_case keys", () => {
    const config = deepgram({ apiKey: "dg_test" });
    expect(config.toDict()).toEqual({
      provider: "deepgram",
      api_key: "dg_test",
      language: "en",
      options: {
        model: "nova-3",
        endpointing_ms: 150,
        utterance_end_ms: 1000,
        smart_format: true,
        interim_results: true,
      },
    });
  });

  it("forwards tuning knobs into options (BUG #13 parity)", () => {
    const config = deepgram({
      apiKey: "dg_test",
      endpointingMs: 80,
      utteranceEndMs: 1500,
      smartFormat: false,
      interimResults: false,
      model: "nova-2",
    });
    expect(config.options).toEqual({
      model: "nova-2",
      endpointing_ms: 80,
      utterance_end_ms: 1500,
      smart_format: false,
      interim_results: false,
    });
  });
});

describe("whisper", () => {
  it("returns STTConfig", () => {
    const config = whisper({ apiKey: "sk_test" });
    expect(config.provider).toBe("whisper");
  });
});

describe("elevenlabs", () => {
  it("returns TTSConfig with default voice", () => {
    const config = elevenlabs({ apiKey: "el_test" });
    expect(config.provider).toBe("elevenlabs");
    expect(config.voice).toBe("rachel");
  });

  it("accepts custom voice", () => {
    const config = elevenlabs({ apiKey: "el_test", voice: "aria" });
    expect(config.voice).toBe("aria");
  });
});

describe("openaiTts", () => {
  it("returns TTSConfig with default voice", () => {
    const config = openaiTts({ apiKey: "sk_test" });
    expect(config.provider).toBe("openai");
    expect(config.voice).toBe("alloy");
  });

  it("accepts custom voice", () => {
    const config = openaiTts({ apiKey: "sk_test", voice: "nova" });
    expect(config.voice).toBe("nova");
  });
});

// ---------------------------------------------------------------------------
// Pipeline providers: DeepgramSTT and ElevenLabsTTS
// ---------------------------------------------------------------------------

describe("DeepgramSTT", () => {
  it("forTwilio() configures mulaw 8kHz", () => {
    const stt = DeepgramSTT.forTwilio("dg_test", "en");
    // Accessing private fields via type assertion for test purposes
    const s = stt as unknown as Record<string, unknown>;
    expect(s["apiKey"]).toBe("dg_test");
    expect(s["encoding"]).toBe("mulaw");
    expect(s["sampleRate"]).toBe(8000);
    expect(s["language"]).toBe("en");
  });

  it("default constructor uses linear16 16kHz", () => {
    const stt = new DeepgramSTT("dg_test", "it");
    const s = stt as unknown as Record<string, unknown>;
    expect(s["encoding"]).toBe("linear16");
    expect(s["sampleRate"]).toBe(16000);
    expect(s["language"]).toBe("it");
  });

  it("stores the api key", () => {
    const stt = new DeepgramSTT("my_key");
    const s = stt as unknown as Record<string, unknown>;
    expect(s["apiKey"]).toBe("my_key");
  });
});

describe("ElevenLabsTTS", () => {
  it("stores api key and voice id", () => {
    const tts = new ElevenLabsTTS("el_test", "21m00Tcm4TlvDq8ikWAM");
    const t = tts as unknown as Record<string, unknown>;
    expect(t["apiKey"]).toBe("el_test");
    expect(t["voiceId"]).toBe("21m00Tcm4TlvDq8ikWAM");
  });

  it("uses default voice id when not specified", () => {
    const tts = new ElevenLabsTTS("el_test");
    const t = tts as unknown as Record<string, unknown>;
    expect(t["voiceId"]).toBe("EXAVITQu4vr4xnSDxMaL");
  });

  it("uses eleven_turbo_v2_5 model by default", () => {
    const tts = new ElevenLabsTTS("el_test");
    const t = tts as unknown as Record<string, unknown>;
    expect(t["modelId"]).toBe("eleven_turbo_v2_5");
  });

  it("uses pcm_16000 output format by default", () => {
    const tts = new ElevenLabsTTS("el_test");
    const t = tts as unknown as Record<string, unknown>;
    expect(t["outputFormat"]).toBe("pcm_16000");
  });
});
