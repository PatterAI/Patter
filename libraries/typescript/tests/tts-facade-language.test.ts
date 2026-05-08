/**
 * Regression tests for the public `getpatter/tts/elevenlabs` facade.
 *
 * The facade wraps `ElevenLabsTTS` and is what users construct in pipeline
 * mode. It must forward `languageCode` / `voiceSettings` to the underlying
 * provider — the original facade signature dropped them silently, breaking
 * the multilingual pipeline path.
 */

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import * as elevenlabs from "../src/tts/elevenlabs";

const ENV_KEY = "ELEVENLABS_API_KEY";

describe("[unit] tts/elevenlabs facade — languageCode forwarding", () => {
  let prev: string | undefined;
  beforeEach(() => {
    prev = process.env[ENV_KEY];
    process.env[ENV_KEY] = "test-key";
  });
  afterEach(() => {
    if (prev === undefined) delete process.env[ENV_KEY];
    else process.env[ENV_KEY] = prev;
  });

  it("forwards languageCode to the provider", () => {
    const tts = new elevenlabs.TTS({ languageCode: "it" });
    // The provider stores `languageCode` as a private readonly field —
    // we rely on the runtime value via the cast.
    const stored = (tts as unknown as { languageCode?: string }).languageCode;
    expect(stored).toBe("it");
  });

  it("forwards voiceSettings to the provider", () => {
    const settings = { stability: 0.4, similarity_boost: 0.7 };
    const tts = new elevenlabs.TTS({ voiceSettings: settings });
    const stored = (tts as unknown as { voiceSettings?: object }).voiceSettings;
    expect(stored).toEqual(settings);
  });

  it("preserves provider defaults when optional kwargs are omitted", () => {
    const tts = new elevenlabs.TTS();
    const stored = tts as unknown as {
      languageCode?: string;
      voiceSettings?: object;
    };
    expect(stored.languageCode).toBeUndefined();
    expect(stored.voiceSettings).toBeUndefined();
  });

  it("forTwilio still works with the carrier-options overload", () => {
    const tts = elevenlabs.TTS.forTwilio();
    const stored = tts as unknown as { outputFormat?: string };
    expect(stored.outputFormat).toBe("ulaw_8000");
  });

  it("resolves apiKey from ELEVENLABS_API_KEY env var", () => {
    process.env[ENV_KEY] = "env-key";
    const tts = new elevenlabs.TTS();
    const stored = (tts as unknown as { apiKey: string }).apiKey;
    expect(stored).toBe("env-key");
  });

  it("explicit apiKey wins over the env var", () => {
    const tts = new elevenlabs.TTS({ apiKey: "explicit-key" });
    const stored = (tts as unknown as { apiKey: string }).apiKey;
    expect(stored).toBe("explicit-key");
  });

  it("throws when no apiKey is available", () => {
    delete process.env[ENV_KEY];
    expect(() => new elevenlabs.TTS()).toThrow(/ELEVENLABS_API_KEY/);
  });
});
