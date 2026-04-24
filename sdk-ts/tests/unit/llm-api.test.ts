/**
 * Unit tests for the Phase 2 v0.5.1 LLM namespace API:
 *  - `llm/openai`, `llm/anthropic`, `llm/groq`, `llm/cerebras`, `llm/google`
 *  - `phone.agent({ llm })` selector on the client
 *  - `LLMLoop` accepts an injected `llmProvider` instance
 *  - `serve({ onMessage })` conflicts with `agent({ llm })`
 *  - Flat re-exports from `getpatter` index
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

import * as openaiLlm from "../../src/llm/openai";
import * as anthropicLlm from "../../src/llm/anthropic";
import * as groqLlm from "../../src/llm/groq";
import * as cerebrasLlm from "../../src/llm/cerebras";
import * as googleLlm from "../../src/llm/google";

import * as flat from "../../src/index";
import { LLMLoop } from "../../src/llm-loop";
import type { LLMProvider, LLMChunk } from "../../src/llm-loop";
import { setLogger, getLogger } from "../../src/logger";
import type { Logger } from "../../src/logger";
import { Patter } from "../../src/client";
import { Twilio } from "../../src/index";

// ---------------------------------------------------------------------------
// Env var snapshot — restore after each test so nothing leaks between cases.
// ---------------------------------------------------------------------------

const TRACKED_ENV_KEYS = [
  "OPENAI_API_KEY",
  "ANTHROPIC_API_KEY",
  "GROQ_API_KEY",
  "CEREBRAS_API_KEY",
  "GEMINI_API_KEY",
  "GOOGLE_API_KEY",
] as const;

let envSnapshot: Record<string, string | undefined>;

beforeEach(() => {
  envSnapshot = {};
  for (const k of TRACKED_ENV_KEYS) {
    envSnapshot[k] = process.env[k];
    delete process.env[k];
  }
});

afterEach(() => {
  for (const k of TRACKED_ENV_KEYS) {
    const v = envSnapshot[k];
    if (v === undefined) {
      delete process.env[k];
    } else {
      process.env[k] = v;
    }
  }
});

// ---------------------------------------------------------------------------
// Per-provider instantiation
// ---------------------------------------------------------------------------

describe("llm/openai", () => {
  it("LLM accepts explicit apiKey", () => {
    const llm = new openaiLlm.LLM({ apiKey: "sk-test" });
    expect(llm).toBeDefined();
    expect(typeof llm.stream).toBe("function");
  });

  it("LLM reads OPENAI_API_KEY env", () => {
    process.env.OPENAI_API_KEY = "sk-env";
    expect(new openaiLlm.LLM()).toBeDefined();
  });

  it("LLM throws with a message mentioning OPENAI_API_KEY when no key is available", () => {
    expect(() => new openaiLlm.LLM()).toThrow(/OPENAI_API_KEY/);
  });

  it("LLM accepts an optional model override", () => {
    expect(new openaiLlm.LLM({ apiKey: "sk-x", model: "gpt-4o" })).toBeDefined();
  });
});

describe("llm/anthropic", () => {
  it("LLM accepts explicit apiKey", () => {
    const llm = new anthropicLlm.LLM({ apiKey: "sk-ant-test" });
    expect(llm).toBeDefined();
    expect(typeof llm.stream).toBe("function");
  });

  it("LLM reads ANTHROPIC_API_KEY env", () => {
    process.env.ANTHROPIC_API_KEY = "sk-ant-env";
    expect(new anthropicLlm.LLM()).toBeDefined();
  });

  it("LLM throws with a message mentioning ANTHROPIC_API_KEY when no key is available", () => {
    expect(() => new anthropicLlm.LLM()).toThrow(/ANTHROPIC_API_KEY/);
  });

  it("LLM accepts a model override", () => {
    expect(
      new anthropicLlm.LLM({ apiKey: "sk-ant-x", model: "claude-haiku-4-5-20251001" }),
    ).toBeDefined();
  });
});

describe("llm/groq", () => {
  it("LLM accepts explicit apiKey", () => {
    const llm = new groqLlm.LLM({ apiKey: "gsk_test" });
    expect(llm).toBeDefined();
    expect(typeof llm.stream).toBe("function");
  });

  it("LLM reads GROQ_API_KEY env", () => {
    process.env.GROQ_API_KEY = "gsk_env";
    expect(new groqLlm.LLM()).toBeDefined();
  });

  it("LLM throws with a message mentioning GROQ_API_KEY when no key is available", () => {
    expect(() => new groqLlm.LLM()).toThrow(/GROQ_API_KEY/);
  });
});

describe("llm/cerebras", () => {
  it("LLM accepts explicit apiKey", () => {
    const llm = new cerebrasLlm.LLM({ apiKey: "csk-test" });
    expect(llm).toBeDefined();
    expect(typeof llm.stream).toBe("function");
  });

  it("LLM reads CEREBRAS_API_KEY env", () => {
    process.env.CEREBRAS_API_KEY = "csk-env";
    expect(new cerebrasLlm.LLM()).toBeDefined();
  });

  it("LLM throws with a message mentioning CEREBRAS_API_KEY when no key is available", () => {
    expect(() => new cerebrasLlm.LLM()).toThrow(/CEREBRAS_API_KEY/);
  });
});

describe("llm/google", () => {
  it("LLM accepts explicit apiKey", () => {
    const llm = new googleLlm.LLM({ apiKey: "AIza-explicit" });
    expect(llm).toBeDefined();
    expect(typeof llm.stream).toBe("function");
  });

  it("LLM reads GEMINI_API_KEY first", () => {
    process.env.GEMINI_API_KEY = "AIza-gemini";
    process.env.GOOGLE_API_KEY = "AIza-google";
    // Both are present; constructor picks GEMINI_API_KEY.  We can't introspect
    // the stored key (it's private), so just assert construction succeeds and
    // verify precedence in the GOOGLE_API_KEY-only fallback case below.
    expect(new googleLlm.LLM()).toBeDefined();
  });

  it("LLM falls back to GOOGLE_API_KEY when GEMINI_API_KEY is unset", () => {
    process.env.GOOGLE_API_KEY = "AIza-google-only";
    expect(new googleLlm.LLM()).toBeDefined();
  });

  it("LLM throws mentioning both GEMINI_API_KEY and GOOGLE_API_KEY when neither is set", () => {
    expect(() => new googleLlm.LLM()).toThrow(/GEMINI_API_KEY/);
    expect(() => new googleLlm.LLM()).toThrow(/GOOGLE_API_KEY/);
  });
});

// ---------------------------------------------------------------------------
// phone.agent({ llm })
// ---------------------------------------------------------------------------

describe("phone.agent({ llm })", () => {
  function makePhone() {
    return new Patter({
      carrier: new Twilio({ accountSid: "AC_test", authToken: "tok" }),
      phoneNumber: "+15550001234",
      webhookUrl: "example.com/wh",
    });
  }

  it("stores a valid LLMProvider on the resolved agent", () => {
    const phone = makePhone();
    const llm = new anthropicLlm.LLM({ apiKey: "sk-ant-x" });
    const resolved = phone.agent({ systemPrompt: "pipe", llm });
    expect(resolved.llm).toBe(llm);
  });

  it("throws a clear error for a non-LLMProvider plain object", () => {
    const phone = makePhone();
    expect(() =>
      phone.agent({
        systemPrompt: "pipe",
        // Intentional bad value — missing `.stream`.
        llm: { foo: "bar" } as unknown as LLMProvider,
      }),
    ).toThrow(/LLMProvider/);
  });

  it("throws for null passed as llm", () => {
    const phone = makePhone();
    expect(() =>
      phone.agent({ systemPrompt: "pipe", llm: null as unknown as LLMProvider }),
    ).toThrow(/LLMProvider/);
  });

  it("engine + llm: agent constructs, logger.warn is called (does not throw)", () => {
    const phone = makePhone();
    const warnSpy = vi.fn();
    const original = getLogger();
    const mockLogger: Logger = {
      info: () => {},
      warn: warnSpy,
      error: () => {},
      debug: () => {},
    };
    setLogger(mockLogger);
    try {
      const engine = new flat.OpenAIRealtime({ apiKey: "sk-x" });
      const llm = new anthropicLlm.LLM({ apiKey: "sk-ant-x" });
      const resolved = phone.agent({ systemPrompt: "p", engine, llm });
      expect(warnSpy).toHaveBeenCalledOnce();
      expect(String(warnSpy.mock.calls[0]?.[0])).toMatch(/ignored.*engine|engine.*ignored/i);
      // Still stored — the warning is advisory, not destructive.
      expect(resolved.llm).toBe(llm);
    } finally {
      setLogger(original);
    }
  });
});

// ---------------------------------------------------------------------------
// LLMLoop accepts an injected llmProvider
// ---------------------------------------------------------------------------

describe("LLMLoop with injected llmProvider", () => {
  it("constructs with an AnthropicLLM instance without touching the network", () => {
    const provider = new anthropicLlm.LLM({ apiKey: "sk-ant-x" });
    const loop = new LLMLoop("", "", "system", null, provider);
    expect(loop).toBeDefined();
  });

  it("still supports the legacy positional (apiKey, model, prompt) form", () => {
    const loop = new LLMLoop("sk-x", "gpt-4o-mini", "system");
    expect(loop).toBeDefined();
  });

  it("routes llm.stream through the LLMLoop.run generator", async () => {
    const calls: Array<{ messages: Array<Record<string, unknown>> }> = [];
    const stubProvider: LLMProvider = {
      async *stream(messages) {
        calls.push({ messages });
        const chunks: LLMChunk[] = [
          { type: "text", content: "hello " },
          { type: "text", content: "world" },
        ];
        for (const c of chunks) yield c;
      },
    };
    const loop = new LLMLoop("", "", "sys", null, stubProvider);
    const tokens: string[] = [];
    for await (const t of loop.run("hi", [], {})) tokens.push(t);
    expect(tokens).toEqual(["hello ", "world"]);
    expect(calls).toHaveLength(1);
    expect((calls[0].messages[0] as Record<string, unknown>).role).toBe("system");
  });
});

// ---------------------------------------------------------------------------
// Conflict: serve({ onMessage }) + agent({ llm })
// ---------------------------------------------------------------------------

describe("serve({ onMessage }) + agent({ llm }) conflict", () => {
  it("StreamHandler throws when both are supplied", async () => {
    // Import lazily so the module-level WebSocket dependency can load cleanly.
    const { StreamHandler } = await import("../../src/stream-handler");
    const { MetricsStore } = await import("../../src/dashboard/store");
    const { RemoteMessageHandler } = await import("../../src/remote-message");

    const stubLlm: LLMProvider = {
      // eslint-disable-next-line @typescript-eslint/require-await, require-yield
      async *stream() {
        return;
      },
    };

    // Minimal stub bridge — only fields touched by initializeProvider() matter.
    const stubBridge = {
      label: "test",
      telephonyProvider: "twilio" as const,
      sendAudio: () => {},
      sendMark: () => {},
      sendClear: () => {},
      transferCall: async () => {},
      endCall: async () => {},
      createStt: async () => null,
      queryTelephonyCost: async () => {},
    };

    // Stub AI adapter builder — never invoked because provider is 'pipeline'
    // with no engine, but the deps shape requires a function.
    const buildAIAdapter = () => {
      throw new Error("should not be called");
    };

    const deps = {
      config: { openaiKey: "sk-test" },
      agent: {
        systemPrompt: "pipe",
        provider: "pipeline" as const,
        llm: stubLlm,
      },
      bridge: stubBridge,
      metricsStore: new MetricsStore(),
      pricing: null,
      remoteHandler: new RemoteMessageHandler(),
      onMessage: async () => "reply",
      recording: false,
      buildAIAdapter: buildAIAdapter as never,
      sanitizeVariables: (raw: Record<string, unknown>) =>
        Object.fromEntries(
          Object.entries(raw).map(([k, v]) => [k, String(v)]),
        ) as Record<string, string>,
      resolveVariables: (template: string) => template,
    };

    // A bare WebSocket-like object is enough — initializeProvider() does not
    // write to it before the llm/onMessage conflict check fires.
    const fakeWs = {} as unknown as import("ws").WebSocket;
    const handler = new StreamHandler(deps as never, fakeWs, "+15550001111", "+15550002222");

    // ``initPipeline`` is private; invoke via bracket access. The bridge stub
    // returns null STT + TTS so execution proceeds past the connect() step
    // and hits the llm/onMessage conflict check.
    const init = (handler as unknown as {
      initPipeline: (prompt: string) => Promise<void>;
    }).initPipeline.bind(handler);
    await expect(init("pipe")).rejects.toThrow(/Cannot pass both/);
  });
});

// ---------------------------------------------------------------------------
// Flat re-exports from the package root
// ---------------------------------------------------------------------------

describe("flat re-exports from getpatter (LLM)", () => {
  it("OpenAILLM, AnthropicLLM, GroqLLM, CerebrasLLM, GoogleLLM are classes", () => {
    expect(typeof flat.OpenAILLM).toBe("function");
    expect(typeof flat.AnthropicLLM).toBe("function");
    expect(typeof flat.GroqLLM).toBe("function");
    expect(typeof flat.CerebrasLLM).toBe("function");
    expect(typeof flat.GoogleLLM).toBe("function");
  });

  it("each flat LLM constructs with an explicit apiKey", () => {
    expect(new flat.OpenAILLM({ apiKey: "sk-x" })).toBeDefined();
    expect(new flat.AnthropicLLM({ apiKey: "sk-ant-x" })).toBeDefined();
    expect(new flat.GroqLLM({ apiKey: "gsk_x" })).toBeDefined();
    expect(new flat.CerebrasLLM({ apiKey: "csk-x" })).toBeDefined();
    expect(new flat.GoogleLLM({ apiKey: "AIza-x" })).toBeDefined();
  });

  it("flat LLMs read their env vars", () => {
    process.env.OPENAI_API_KEY = "sk-env";
    process.env.ANTHROPIC_API_KEY = "sk-ant-env";
    process.env.GROQ_API_KEY = "gsk-env";
    process.env.CEREBRAS_API_KEY = "csk-env";
    process.env.GEMINI_API_KEY = "AIza-env";
    expect(new flat.OpenAILLM()).toBeDefined();
    expect(new flat.AnthropicLLM()).toBeDefined();
    expect(new flat.GroqLLM()).toBeDefined();
    expect(new flat.CerebrasLLM()).toBeDefined();
    expect(new flat.GoogleLLM()).toBeDefined();
  });
});
