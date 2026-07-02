import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  SarvamTTS,
  SarvamModel,
  SarvamLanguage,
} from "../../src/providers/sarvam-tts";
import { TTS as SarvamPipelineTTS } from "../../src/tts/sarvam";

/**
 * [mocked] Sarvam AI TTS — request shape, per-model field gating, base64
 * decoding and telephony factories.
 *
 * The Sarvam REST endpoint is mocked at the global `fetch` boundary; every
 * other code path (payload assembly, model gating, base64 decoding) runs for
 * real. NO PII: we only round-trip the mock's bytes.
 */
describe("[mocked] Sarvam AI TTS", () => {
  const ORIGINAL_FETCH = global.fetch;
  let lastBody: Record<string, unknown> | null = null;
  let lastHeaders: Record<string, string> | null = null;
  let lastUrl: string | null = null;

  function jsonAudioResponse(...payloads: string[]): Response {
    const audios = payloads.map((p) => Buffer.from(p).toString("base64"));
    return new Response(JSON.stringify({ request_id: "req-1", audios }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }

  beforeEach(() => {
    lastBody = null;
    lastHeaders = null;
    lastUrl = null;
    global.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      lastUrl = typeof input === "string" ? input : input.toString();
      lastBody = JSON.parse(init?.body as string);
      lastHeaders = init?.headers as Record<string, string>;
      return jsonAudioResponse("hello", "world");
    };
  });

  afterEach(() => {
    global.fetch = ORIGINAL_FETCH;
  });

  describe("low-level SarvamTTS", () => {
    it("posts to the REST endpoint with the api-subscription-key header", async () => {
      const tts = new SarvamTTS("key");
      await tts.synthesize("namaste");
      expect(lastUrl).toBe("https://api.sarvam.ai/text-to-speech");
      expect(lastHeaders?.["api-subscription-key"]).toBe("key");
      expect(lastHeaders?.["Content-Type"]).toBe("application/json");
    });

    it("defaults to bulbul:v3, speaker shubh, linear16 @ 16 kHz, en-IN", async () => {
      const tts = new SarvamTTS("key");
      await tts.synthesize("hi");
      expect(lastBody?.model).toBe(SarvamModel.BULBUL_V3);
      expect(lastBody?.speaker).toBe("shubh");
      expect(lastBody?.target_language_code).toBe(SarvamLanguage.ENGLISH);
      expect(lastBody?.output_audio_codec).toBe("linear16");
      expect(lastBody?.speech_sample_rate).toBe(16000);
      for (const key of [
        "pace",
        "pitch",
        "loudness",
        "temperature",
        "enable_preprocessing",
        "dict_id",
      ]) {
        expect(lastBody).not.toHaveProperty(key);
      }
    });

    it("maps the language option to target_language_code", async () => {
      const tts = new SarvamTTS("key", { language: "hi-IN" });
      await tts.synthesize("hi");
      expect(lastBody?.target_language_code).toBe("hi-IN");
    });

    it("sends pace only when set", async () => {
      const tts = new SarvamTTS("key", { pace: 1.25 });
      await tts.synthesize("hi");
      expect(lastBody?.pace).toBe(1.25);
    });

    it("gates pitch/loudness/enablePreprocessing to bulbul:v2 (drops temperature)", async () => {
      const tts = new SarvamTTS("key", {
        model: "bulbul:v2",
        speaker: "anushka",
        pitch: 0.2,
        loudness: 1.5,
        enablePreprocessing: true,
        temperature: 0.9, // v3-only → must be dropped
      });
      await tts.synthesize("hi");
      expect(lastBody?.model).toBe("bulbul:v2");
      expect(lastBody?.pitch).toBe(0.2);
      expect(lastBody?.loudness).toBe(1.5);
      expect(lastBody?.enable_preprocessing).toBe(true);
      expect(lastBody).not.toHaveProperty("temperature");
      expect(lastBody).not.toHaveProperty("dict_id");
    });

    it("gates temperature/dictId to bulbul:v3 (drops pitch/loudness)", async () => {
      const tts = new SarvamTTS("key", {
        model: "bulbul:v3",
        temperature: 0.8,
        dictId: "dict-42",
        pitch: 0.2, // v2-only → must be dropped
        loudness: 1.5,
        enablePreprocessing: true,
      });
      await tts.synthesize("hi");
      expect(lastBody?.temperature).toBe(0.8);
      expect(lastBody?.dict_id).toBe("dict-42");
      expect(lastBody).not.toHaveProperty("pitch");
      expect(lastBody).not.toHaveProperty("loudness");
      expect(lastBody).not.toHaveProperty("enable_preprocessing");
    });

    it("decodes base64 audios and concatenates in order", async () => {
      const tts = new SarvamTTS("key");
      const out = await tts.synthesize("hi");
      expect(out.toString("utf8")).toBe("helloworld");
    });

    it("yields one Buffer per audios entry via synthesizeStream", async () => {
      const tts = new SarvamTTS("key");
      const collected: string[] = [];
      for await (const chunk of tts.synthesizeStream("hi")) {
        collected.push(chunk.toString("utf8"));
      }
      expect(collected).toEqual(["hello", "world"]);
    });

    it("yields nothing on a malformed body", async () => {
      global.fetch = async () =>
        new Response(JSON.stringify({ request_id: "x" }), { status: 200 });
      const tts = new SarvamTTS("key");
      const out = await tts.synthesize("hi");
      expect(out.length).toBe(0);
    });

    it("surfaces non-200 responses with status + body", async () => {
      global.fetch = async () => new Response("rate limited", { status: 429 });
      const tts = new SarvamTTS("key");
      await expect(tts.synthesize("hi")).rejects.toThrow(/Sarvam TTS error 429/);
    });

    it("throws when constructed without an apiKey", () => {
      expect(() => new SarvamTTS("")).toThrow(/apiKey is required/);
    });
  });

  describe("telephony factories", () => {
    it("forTwilio emits μ-law @ 8 kHz natively (carrier-wire passthrough)", async () => {
      const tts = SarvamTTS.forTwilio("key");
      await tts.synthesize("hi");
      expect(lastBody?.output_audio_codec).toBe("mulaw");
      expect(lastBody?.speech_sample_rate).toBe(8000);
      expect(tts.sourceAudioFormat()).toEqual({ encoding: "mulaw", sampleRate: 8000 });
    });

    it("forTwilio honours overrides but pins the μ-law 8 kHz wire", async () => {
      const tts = SarvamTTS.forTwilio("key", { language: "ta-IN", speaker: "kavya" });
      await tts.synthesize("hi");
      expect(lastBody?.output_audio_codec).toBe("mulaw");
      expect(lastBody?.speech_sample_rate).toBe(8000);
      expect(lastBody?.target_language_code).toBe("ta-IN");
      expect(lastBody?.speaker).toBe("kavya");
    });

    it("forTelnyx emits μ-law @ 8 kHz natively (PCMU wire passthrough)", async () => {
      const tts = SarvamTTS.forTelnyx("key");
      await tts.synthesize("hi");
      expect(lastBody?.output_audio_codec).toBe("mulaw");
      expect(lastBody?.speech_sample_rate).toBe(8000);
    });

    it("constructor default unchanged (linear16 @ 16000)", async () => {
      const tts = new SarvamTTS("key");
      await tts.synthesize("hi");
      expect(lastBody?.output_audio_codec).toBe("linear16");
      expect(lastBody?.speech_sample_rate).toBe(16000);
      expect(tts.sourceAudioFormat()).toEqual({
        encoding: "pcm_s16le",
        sampleRate: 16000,
      });
    });
  });

  describe("pipeline-mode TTS class", () => {
    it("requires SARVAM_API_KEY (or apiKey opt) and forwards to provider", async () => {
      const prev = process.env.SARVAM_API_KEY;
      delete process.env.SARVAM_API_KEY;
      try {
        expect(() => new SarvamPipelineTTS()).toThrow(/SARVAM_API_KEY/);

        const tts = new SarvamPipelineTTS({ apiKey: "tok-from-opt" });
        await tts.synthesize("hi");
        expect(lastHeaders?.["api-subscription-key"]).toBe("tok-from-opt");

        process.env.SARVAM_API_KEY = "tok-from-env";
        const tts2 = new SarvamPipelineTTS();
        await tts2.synthesize("hi");
        expect(lastHeaders?.["api-subscription-key"]).toBe("tok-from-env");
      } finally {
        if (prev === undefined) delete process.env.SARVAM_API_KEY;
        else process.env.SARVAM_API_KEY = prev;
      }
    });

    it("forTwilio (options-object form) emits μ-law @ 8 kHz natively", async () => {
      const tts = SarvamPipelineTTS.forTwilio({ apiKey: "key" });
      await tts.synthesize("hi");
      expect(lastBody?.output_audio_codec).toBe("mulaw");
      expect(lastBody?.speech_sample_rate).toBe(8000);
    });

    it("forTwilio (positional form) is parent-compatible", async () => {
      const tts = SarvamPipelineTTS.forTwilio("key");
      await tts.synthesize("hi");
      expect(lastBody?.output_audio_codec).toBe("mulaw");
      expect(lastBody?.speech_sample_rate).toBe(8000);
    });

    it("forTwilio falls back to SARVAM_API_KEY env var", async () => {
      const prev = process.env.SARVAM_API_KEY;
      process.env.SARVAM_API_KEY = "env-key";
      try {
        const tts = SarvamPipelineTTS.forTwilio();
        await tts.synthesize("hi");
        expect(lastHeaders?.["api-subscription-key"]).toBe("env-key");
        expect(lastBody?.speech_sample_rate).toBe(8000);
      } finally {
        if (prev === undefined) delete process.env.SARVAM_API_KEY;
        else process.env.SARVAM_API_KEY = prev;
      }
    });

    it("forTwilio without apiKey or env throws", () => {
      const prev = process.env.SARVAM_API_KEY;
      delete process.env.SARVAM_API_KEY;
      try {
        expect(() => SarvamPipelineTTS.forTwilio()).toThrow(/SARVAM_API_KEY/);
      } finally {
        if (prev !== undefined) process.env.SARVAM_API_KEY = prev;
      }
    });
  });
});
