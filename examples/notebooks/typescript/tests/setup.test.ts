import { describe, it, expect, beforeEach, afterEach } from "vitest";
import * as fs from "node:fs";
import * as path from "node:path";
import * as os from "node:os";

import {
  load, hasKey, NotebookSkip, skip, printKeyMatrix, cell, loadFixture,
  runStt, runTts,
} from "../_setup";

let tmpDir: string;
const ORIGINAL_ENV = { ...process.env };

beforeEach(() => {
  tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "patter-nb-"));
});

afterEach(() => {
  fs.rmSync(tmpDir, { recursive: true, force: true });
  process.env = { ...ORIGINAL_ENV };
});

describe("[unit] _setup load()", () => {
  it("reads keys from the .env file path", () => {
    const envFile = path.join(tmpDir, ".env");
    fs.writeFileSync(envFile, "OPENAI_API_KEY=sk-x\nENABLE_LIVE_CALLS=1\n");
    delete process.env.OPENAI_API_KEY;
    delete process.env.ENABLE_LIVE_CALLS;
    const env = load({ envFile });
    expect(env.openaiKey).toBe("sk-x");
    expect(env.enableLiveCalls).toBe(true);
  });

  it("returns empty strings for missing keys", () => {
    delete process.env.OPENAI_API_KEY;
    delete process.env.ENABLE_LIVE_CALLS;
    const env = load({ envFile: path.join(tmpDir, "nope.env") });
    expect(env.openaiKey).toBe("");
    expect(env.enableLiveCalls).toBe(false);
  });
});

describe("[unit] _setup hasKey + skip + cell + loadFixture", () => {
  it("hasKey returns true when env var set", () => {
    process.env.OPENAI_API_KEY = "x";
    const env = load({ envFile: path.join(tmpDir, "missing.env") });
    expect(hasKey(env, "OPENAI_API_KEY")).toBe(true);
  });

  it("skip throws NotebookSkip", () => {
    expect(() => skip("missing key")).toThrowError(NotebookSkip);
  });

  it("cell skips on missing key and continues", async () => {
    delete process.env.OPENAI_API_KEY;
    const env = load({ envFile: path.join(tmpDir, "missing.env") });
    let ran = false;
    await cell("test_cell", { tier: 3, required: ["OPENAI_API_KEY"], env }, async () => {
      ran = true;
    });
    expect(ran).toBe(false);
  });

  it("cell skips T4 when ENABLE_LIVE_CALLS=0", async () => {
    process.env.ENABLE_LIVE_CALLS = "0";
    const env = load({ envFile: path.join(tmpDir, "missing.env") });
    let ran = false;
    await cell("live_cell", { tier: 4, env }, async () => { ran = true; });
    expect(ran).toBe(false);
  });

  it("cell swallows exceptions and continues", async () => {
    const env = load({ envFile: path.join(tmpDir, "missing.env") });
    await cell("test_cell", { tier: 1, env }, async () => {
      throw new Error("kaboom");
    });
  });

  it("loadFixture returns bytes for known file", () => {
    const data = loadFixture("audio/hello_world_16khz_pcm.wav");
    expect(data.length).toBeGreaterThan(100);
  });

  it("printKeyMatrix prints check + circle markers", () => {
    process.env.OPENAI_API_KEY = "x";
    delete process.env.DEEPGRAM_API_KEY;
    const env = load({ envFile: path.join(tmpDir, "missing.env") });
    const orig = console.log;
    const buf: string[] = [];
    console.log = (m: string) => buf.push(m);
    try {
      printKeyMatrix(env, ["OPENAI_API_KEY", "DEEPGRAM_API_KEY"]);
    } finally {
      console.log = orig;
    }
    const out = buf.join("\n");
    expect(out).toContain("OPENAI_API_KEY");
    expect(out).toContain("✅");
    expect(out).toContain("DEEPGRAM_API_KEY");
    expect(out).toContain("⚪");
  });
});

describe("[unit] runStt + runTts", () => {
  it("runStt aggregates transcripts from a fake provider", async () => {
    const fake = {
      connect: async () => {},
      sendAudio: async (_b: Buffer) => {},
      close: async () => {},
      receiveTranscripts: async function* () {
        yield "hello "; yield "world";
      },
    };
    const t = await runStt(fake as any, Buffer.alloc(16000));
    expect(t.trim()).toBe("hello world");
  });

  it("runTts concatenates chunks", async () => {
    const fake = {
      synthesize: async function* (_t: string) {
        yield Buffer.from([1, 2]); yield Buffer.from([3, 4]);
      },
    };
    const audio = await runTts(fake as any, "hi");
    expect(Array.from(audio)).toEqual([1, 2, 3, 4]);
  });
});
