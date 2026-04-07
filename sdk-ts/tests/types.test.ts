import { describe, it, expect } from "vitest";
import { deepgram, elevenlabs } from "../src/providers";

describe("STTConfig", () => {
  it("toDict includes all fields", () => {
    const config = deepgram({ apiKey: "dg_test", language: "it" });
    expect(config.toDict()).toEqual({
      provider: "deepgram",
      api_key: "dg_test",
      language: "it",
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
