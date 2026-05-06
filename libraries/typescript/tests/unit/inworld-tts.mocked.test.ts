import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { InworldTTS, InworldModel } from "../../src/providers/inworld-tts";
import { TTS as InworldPipelineTTS } from "../../src/tts/inworld";

/**
 * [mocked] Inworld TTS — request shape, NDJSON parsing and base64 decoding.
 *
 * The Inworld REST endpoint is mocked at the global `fetch` boundary; every
 * other code path (payload assembly, NDJSON splitting, base64 decoding) runs
 * for real.
 */
describe("[mocked] Inworld TTS — request + NDJSON parsing", () => {
  const ORIGINAL_FETCH = global.fetch;
  let lastBody: Record<string, unknown> | null = null;
  let lastHeaders: Record<string, string> | null = null;
  let lastUrl: string | null = null;

  function mockNdjsonResponse(lines: string[]): Response {
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        const enc = new TextEncoder();
        for (const line of lines) {
          controller.enqueue(enc.encode(line + "\n"));
        }
        controller.close();
      },
    });
    return new Response(stream, {
      status: 200,
      headers: { "Content-Type": "application/x-ndjson" },
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
      // Two audio chunks + a timestamp-only line we should ignore.
      const chunkA = Buffer.from("hello").toString("base64");
      const chunkB = Buffer.from("world").toString("base64");
      return mockNdjsonResponse([
        JSON.stringify({ result: { audioContent: chunkA } }),
        JSON.stringify({ result: { timestampInfo: { wordAlignment: [] } } }),
        JSON.stringify({ result: { audioContent: chunkB } }),
      ]);
    };
  });

  afterEach(() => {
    global.fetch = ORIGINAL_FETCH;
  });

  describe("low-level InworldTTS", () => {
    it("posts to the streaming endpoint with the Basic auth header", async () => {
      const tts = new InworldTTS("base64-token");
      await tts.synthesize("ciao");
      expect(lastUrl).toBe("https://api.inworld.ai/tts/v1/voice:stream");
      expect(lastHeaders?.Authorization).toBe("Basic base64-token");
      expect(lastHeaders?.["Content-Type"]).toBe("application/json");
    });

    it("defaults to TTS-2, voice Ashley, PCM @ 16 kHz", async () => {
      const tts = new InworldTTS("tok");
      await tts.synthesize("hi");
      expect(lastBody?.modelId).toBe(InworldModel.TTS_2);
      expect(lastBody?.voiceId).toBe("Ashley");
      expect(lastBody?.speakingRate).toBe(1.0);
      const audioConfig = lastBody?.audioConfig as Record<string, unknown>;
      expect(audioConfig.audioEncoding).toBe("PCM");
      expect(audioConfig.sampleRateHertz).toBe(16000);
      expect(audioConfig.bitrate).toBe(64000);
    });

    it("forwards optional language / temperature / deliveryMode only when set", async () => {
      const tts = new InworldTTS("tok");
      await tts.synthesize("hi");
      expect(lastBody).not.toHaveProperty("language");
      expect(lastBody).not.toHaveProperty("temperature");
      expect(lastBody).not.toHaveProperty("deliveryMode");

      const tts2 = new InworldTTS("tok", {
        language: "it",
        temperature: 0.7,
        deliveryMode: "BALANCED",
      });
      await tts2.synthesize("ciao");
      expect(lastBody?.language).toBe("it");
      expect(lastBody?.temperature).toBe(0.7);
      expect(lastBody?.deliveryMode).toBe("BALANCED");
    });

    it("decodes base64 audio and yields concatenated PCM in stream order", async () => {
      const tts = new InworldTTS("tok");
      const out = await tts.synthesize("hi");
      expect(out.toString("utf8")).toBe("helloworld");
    });

    it("yields one Buffer per audio line via synthesizeStream", async () => {
      const tts = new InworldTTS("tok");
      const collected: string[] = [];
      for await (const chunk of tts.synthesizeStream("hi")) {
        collected.push(chunk.toString("utf8"));
      }
      // Timestamp-only line is filtered out.
      expect(collected).toEqual(["hello", "world"]);
    });

    it("surfaces non-200 responses with status + body", async () => {
      global.fetch = async () =>
        new Response("rate limited", { status: 429 });
      const tts = new InworldTTS("tok");
      await expect(tts.synthesize("hi")).rejects.toThrow(/Inworld TTS error 429/);
    });
  });

  describe("pipeline-mode TTS class", () => {
    it("requires INWORLD_API_KEY (or apiKey opt) and forwards to provider", async () => {
      const prev = process.env.INWORLD_API_KEY;
      delete process.env.INWORLD_API_KEY;
      try {
        expect(() => new InworldPipelineTTS()).toThrow(/INWORLD_API_KEY/);

        const tts = new InworldPipelineTTS({ apiKey: "tok-from-opt" });
        await tts.synthesize("hi");
        expect(lastHeaders?.Authorization).toBe("Basic tok-from-opt");

        process.env.INWORLD_API_KEY = "tok-from-env";
        const tts2 = new InworldPipelineTTS();
        await tts2.synthesize("hi");
        expect(lastHeaders?.Authorization).toBe("Basic tok-from-env");
      } finally {
        if (prev === undefined) delete process.env.INWORLD_API_KEY;
        else process.env.INWORLD_API_KEY = prev;
      }
    });
  });
});
