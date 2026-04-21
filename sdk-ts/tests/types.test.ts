import { describe, it, expect } from "vitest";
import { deepgram, elevenlabs } from "../src/providers";

describe("STTConfig", () => {
  it("toDict includes all fields", () => {
    const config = deepgram({ apiKey: "dg_test", language: "it" });
    // BUG #13 parity — the Deepgram factory now carries tuning defaults in
    // ``options`` so callers can pass them through without monkey-patching.
    expect(config.toDict()).toEqual({
      provider: "deepgram",
      api_key: "dg_test",
      language: "it",
      options: {
        model: "nova-3",
        endpointing_ms: 150,
        utterance_end_ms: 1000,
        smart_format: true,
        interim_results: true,
      },
    });
  });

  it("is readonly", () => {
    const config = deepgram({ apiKey: "dg_test" });
    expect(config.provider).toBe("deepgram");
    expect(config.apiKey).toBe("dg_test");
    expect(config.language).toBe("en");
  });
});

describe("TTSConfig", () => {
  it("toDict includes all fields", () => {
    const config = elevenlabs({ apiKey: "el_test", voice: "aria" });
    expect(config.toDict()).toEqual({
      provider: "elevenlabs",
      api_key: "el_test",
      voice: "aria",
    });
  });
});
