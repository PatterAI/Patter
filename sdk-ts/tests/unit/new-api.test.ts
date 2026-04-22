/**
 * Unit tests for the Phase 1a v0.5.0 namespaced API:
 *  - STT classes (`stt/deepgram`, `stt/whisper`, `stt/cartesia`,
 *    `stt/soniox`, `stt/assemblyai`)
 *  - TTS classes (`tts/elevenlabs`, `tts/openai`, `tts/cartesia`,
 *    `tts/rime`, `tts/lmnt`)
 *  - Carriers (`carriers/twilio`, `carriers/telnyx`)
 *  - Engines (`engines/openai` Realtime, `engines/elevenlabs` ConvAI)
 *  - Tunnels (`tunnels` CloudflareTunnel / Static)
 *  - `Tool` / `Guardrail` classes + `tool()` / `guardrail()` factories
 *  - Flat re-exports from `getpatter` index
 */
import { describe, it, expect, beforeEach, afterEach } from "vitest";

import * as deepgramStt from "../../src/stt/deepgram";
import * as whisperStt from "../../src/stt/whisper";
import * as cartesiaStt from "../../src/stt/cartesia";
import * as sonioxStt from "../../src/stt/soniox";
import * as assemblyaiStt from "../../src/stt/assemblyai";

import * as elevenlabsTts from "../../src/tts/elevenlabs";
import * as openaiTts from "../../src/tts/openai";
import * as cartesiaTts from "../../src/tts/cartesia";
import * as rimeTts from "../../src/tts/rime";
import * as lmntTts from "../../src/tts/lmnt";

import * as twilioCarrier from "../../src/carriers/twilio";
import * as telnyxCarrier from "../../src/carriers/telnyx";

import * as openaiEngine from "../../src/engines/openai";
import * as elevenlabsEngine from "../../src/engines/elevenlabs";

import { CloudflareTunnel, Static as StaticTunnel } from "../../src/tunnels";

import { Tool, Guardrail, tool, guardrail } from "../../src/public-api";

import * as flat from "../../src/index";

// ---------------------------------------------------------------------------
// Env var snapshot — restore after each test so nothing leaks between cases.
// ---------------------------------------------------------------------------

const TRACKED_ENV_KEYS = [
  "DEEPGRAM_API_KEY",
  "OPENAI_API_KEY",
  "CARTESIA_API_KEY",
  "SONIOX_API_KEY",
  "ASSEMBLYAI_API_KEY",
  "ELEVENLABS_API_KEY",
  "ELEVENLABS_AGENT_ID",
  "RIME_API_KEY",
  "LMNT_API_KEY",
  "TWILIO_ACCOUNT_SID",
  "TWILIO_AUTH_TOKEN",
  "TELNYX_API_KEY",
  "TELNYX_CONNECTION_ID",
  "TELNYX_PUBLIC_KEY",
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
// STT classes
// ---------------------------------------------------------------------------

describe("stt namespaces", () => {
  it("deepgram.STT accepts explicit apiKey", () => {
    const stt = new deepgramStt.STT({ apiKey: "dg_test" });
    expect(stt).toBeDefined();
  });

  it("deepgram.STT reads DEEPGRAM_API_KEY env", () => {
    process.env.DEEPGRAM_API_KEY = "dg_env";
    const stt = new deepgramStt.STT();
    expect(stt).toBeDefined();
  });

  it("deepgram.STT throws when no key is available", () => {
    expect(() => new deepgramStt.STT()).toThrow(/DEEPGRAM_API_KEY/);
  });

  it("whisper.STT accepts explicit apiKey", () => {
    expect(new whisperStt.STT({ apiKey: "sk-test" })).toBeDefined();
  });

  it("whisper.STT reads OPENAI_API_KEY env", () => {
    process.env.OPENAI_API_KEY = "sk-env";
    expect(new whisperStt.STT()).toBeDefined();
  });

  it("whisper.STT throws when no key is available", () => {
    expect(() => new whisperStt.STT()).toThrow(/OPENAI_API_KEY/);
  });

  it("cartesia.STT accepts explicit apiKey", () => {
    expect(new cartesiaStt.STT({ apiKey: "c_test" })).toBeDefined();
  });

  it("cartesia.STT reads CARTESIA_API_KEY env", () => {
    process.env.CARTESIA_API_KEY = "c_env";
    expect(new cartesiaStt.STT()).toBeDefined();
  });

  it("cartesia.STT throws when no key is available", () => {
    expect(() => new cartesiaStt.STT()).toThrow(/CARTESIA_API_KEY/);
  });

  it("soniox.STT accepts explicit apiKey", () => {
    expect(new sonioxStt.STT({ apiKey: "s_test" })).toBeDefined();
  });

  it("soniox.STT reads SONIOX_API_KEY env", () => {
    process.env.SONIOX_API_KEY = "s_env";
    expect(new sonioxStt.STT()).toBeDefined();
  });

  it("soniox.STT throws when no key is available", () => {
    expect(() => new sonioxStt.STT()).toThrow(/SONIOX_API_KEY/);
  });

  it("assemblyai.STT accepts explicit apiKey", () => {
    expect(new assemblyaiStt.STT({ apiKey: "a_test" })).toBeDefined();
  });

  it("assemblyai.STT reads ASSEMBLYAI_API_KEY env", () => {
    process.env.ASSEMBLYAI_API_KEY = "a_env";
    expect(new assemblyaiStt.STT()).toBeDefined();
  });

  it("assemblyai.STT throws when no key is available", () => {
    expect(() => new assemblyaiStt.STT()).toThrow(/ASSEMBLYAI_API_KEY/);
  });
});

// ---------------------------------------------------------------------------
// TTS classes
// ---------------------------------------------------------------------------

describe("tts namespaces", () => {
  it("elevenlabs.TTS accepts explicit apiKey", () => {
    expect(new elevenlabsTts.TTS({ apiKey: "el_test" })).toBeDefined();
  });

  it("elevenlabs.TTS reads ELEVENLABS_API_KEY env", () => {
    process.env.ELEVENLABS_API_KEY = "el_env";
    expect(new elevenlabsTts.TTS()).toBeDefined();
  });

  it("elevenlabs.TTS throws when no key is available", () => {
    expect(() => new elevenlabsTts.TTS()).toThrow(/ELEVENLABS_API_KEY/);
  });

  it("openai.TTS accepts explicit apiKey", () => {
    expect(new openaiTts.TTS({ apiKey: "sk-test" })).toBeDefined();
  });

  it("openai.TTS reads OPENAI_API_KEY env", () => {
    process.env.OPENAI_API_KEY = "sk-env";
    expect(new openaiTts.TTS()).toBeDefined();
  });

  it("openai.TTS throws when no key is available", () => {
    expect(() => new openaiTts.TTS()).toThrow(/OPENAI_API_KEY/);
  });

  it("cartesia.TTS accepts explicit apiKey", () => {
    expect(new cartesiaTts.TTS({ apiKey: "c_test" })).toBeDefined();
  });

  it("cartesia.TTS reads CARTESIA_API_KEY env", () => {
    process.env.CARTESIA_API_KEY = "c_env";
    expect(new cartesiaTts.TTS()).toBeDefined();
  });

  it("cartesia.TTS throws when no key is available", () => {
    expect(() => new cartesiaTts.TTS()).toThrow(/CARTESIA_API_KEY/);
  });

  it("rime.TTS accepts explicit apiKey", () => {
    expect(new rimeTts.TTS({ apiKey: "r_test" })).toBeDefined();
  });

  it("rime.TTS reads RIME_API_KEY env", () => {
    process.env.RIME_API_KEY = "r_env";
    expect(new rimeTts.TTS()).toBeDefined();
  });

  it("rime.TTS throws when no key is available", () => {
    expect(() => new rimeTts.TTS()).toThrow(/RIME_API_KEY/);
  });

  it("lmnt.TTS accepts explicit apiKey", () => {
    expect(new lmntTts.TTS({ apiKey: "l_test" })).toBeDefined();
  });

  it("lmnt.TTS reads LMNT_API_KEY env", () => {
    process.env.LMNT_API_KEY = "l_env";
    expect(new lmntTts.TTS()).toBeDefined();
  });

  it("lmnt.TTS throws when no key is available", () => {
    expect(() => new lmntTts.TTS()).toThrow(/LMNT_API_KEY/);
  });
});

// ---------------------------------------------------------------------------
// Carriers
// ---------------------------------------------------------------------------

describe("carriers", () => {
  it("twilio.Carrier accepts explicit credentials", () => {
    const c = new twilioCarrier.Carrier({ accountSid: "ACabc", authToken: "tok" });
    expect(c.kind).toBe("twilio");
    expect(c.accountSid).toBe("ACabc");
    expect(c.authToken).toBe("tok");
  });

  it("twilio.Carrier reads env vars", () => {
    process.env.TWILIO_ACCOUNT_SID = "ACenv";
    process.env.TWILIO_AUTH_TOKEN = "tokenv";
    const c = new twilioCarrier.Carrier();
    expect(c.accountSid).toBe("ACenv");
    expect(c.authToken).toBe("tokenv");
  });

  it("twilio.Carrier throws when sid is missing", () => {
    process.env.TWILIO_AUTH_TOKEN = "tok";
    expect(() => new twilioCarrier.Carrier()).toThrow(/TWILIO_ACCOUNT_SID/);
  });

  it("twilio.Carrier throws when token is missing", () => {
    process.env.TWILIO_ACCOUNT_SID = "ACabc";
    expect(() => new twilioCarrier.Carrier()).toThrow(/TWILIO_AUTH_TOKEN/);
  });

  it("telnyx.Carrier accepts explicit credentials", () => {
    const c = new telnyxCarrier.Carrier({
      apiKey: "KEY123",
      connectionId: "conn",
      publicKey: "pub",
    });
    expect(c.kind).toBe("telnyx");
    expect(c.apiKey).toBe("KEY123");
    expect(c.connectionId).toBe("conn");
    expect(c.publicKey).toBe("pub");
  });

  it("telnyx.Carrier reads env vars (publicKey optional)", () => {
    process.env.TELNYX_API_KEY = "KEYenv";
    process.env.TELNYX_CONNECTION_ID = "connenv";
    const c = new telnyxCarrier.Carrier();
    expect(c.apiKey).toBe("KEYenv");
    expect(c.connectionId).toBe("connenv");
    expect(c.publicKey).toBeUndefined();
  });

  it("telnyx.Carrier throws when apiKey is missing", () => {
    process.env.TELNYX_CONNECTION_ID = "connenv";
    expect(() => new telnyxCarrier.Carrier()).toThrow(/TELNYX_API_KEY/);
  });

  it("telnyx.Carrier throws when connectionId is missing", () => {
    process.env.TELNYX_API_KEY = "KEY";
    expect(() => new telnyxCarrier.Carrier()).toThrow(/TELNYX_CONNECTION_ID/);
  });
});

// ---------------------------------------------------------------------------
// Engines
// ---------------------------------------------------------------------------

describe("engines", () => {
  it("openai.Realtime accepts explicit apiKey", () => {
    const r = new openaiEngine.Realtime({ apiKey: "sk-test", voice: "alloy" });
    expect(r.kind).toBe("openai_realtime");
    expect(r.voice).toBe("alloy");
  });

  it("openai.Realtime reads OPENAI_API_KEY env", () => {
    process.env.OPENAI_API_KEY = "sk-env";
    const r = new openaiEngine.Realtime();
    expect(r.apiKey).toBe("sk-env");
  });

  it("openai.Realtime throws when no key is available", () => {
    expect(() => new openaiEngine.Realtime()).toThrow(/OPENAI_API_KEY/);
  });

  it("elevenlabs.ConvAI accepts explicit credentials", () => {
    const c = new elevenlabsEngine.ConvAI({ apiKey: "el_test", agentId: "agent_1" });
    expect(c.kind).toBe("elevenlabs_convai");
    expect(c.agentId).toBe("agent_1");
  });

  it("elevenlabs.ConvAI reads env vars", () => {
    process.env.ELEVENLABS_API_KEY = "el_env";
    process.env.ELEVENLABS_AGENT_ID = "agent_env";
    const c = new elevenlabsEngine.ConvAI();
    expect(c.apiKey).toBe("el_env");
    expect(c.agentId).toBe("agent_env");
  });

  it("elevenlabs.ConvAI throws when apiKey is missing", () => {
    process.env.ELEVENLABS_AGENT_ID = "agent";
    expect(() => new elevenlabsEngine.ConvAI()).toThrow(/ELEVENLABS_API_KEY/);
  });

  it("elevenlabs.ConvAI throws when agentId is missing", () => {
    process.env.ELEVENLABS_API_KEY = "el_test";
    expect(() => new elevenlabsEngine.ConvAI()).toThrow(/ELEVENLABS_AGENT_ID/);
  });
});

// ---------------------------------------------------------------------------
// Tunnels
// ---------------------------------------------------------------------------

describe("tunnels", () => {
  it("CloudflareTunnel instantiates", () => {
    const t = new CloudflareTunnel();
    expect(t.kind).toBe("cloudflare");
  });

  it("Static tunnel takes a hostname", () => {
    const t = new StaticTunnel({ hostname: "agent.example.com" });
    expect(t.kind).toBe("static");
    expect(t.hostname).toBe("agent.example.com");
  });

  it("Static tunnel rejects empty hostname", () => {
    expect(() => new StaticTunnel({ hostname: "" })).toThrow(/hostname/);
  });
});

// ---------------------------------------------------------------------------
// Tool / Guardrail
// ---------------------------------------------------------------------------

describe("Tool / Guardrail classes", () => {
  it("Tool with handler instantiates", () => {
    const t = new Tool({
      name: "test",
      description: "desc",
      handler: async () => "ok",
    });
    expect(t.name).toBe("test");
    expect(typeof t.handler).toBe("function");
    expect(t.webhookUrl).toBeUndefined();
  });

  it("Tool with webhookUrl instantiates", () => {
    const t = new Tool({
      name: "test",
      webhookUrl: "https://example.com/hook",
    });
    expect(t.webhookUrl).toBe("https://example.com/hook");
    expect(t.handler).toBeUndefined();
  });

  it("Tool throws with neither handler nor webhookUrl", () => {
    expect(() => new Tool({ name: "test" })).toThrow(/handler or webhookUrl/);
  });

  it("Tool throws when both handler and webhookUrl are provided", () => {
    expect(
      () =>
        new Tool({
          name: "test",
          handler: async () => "x",
          webhookUrl: "https://example.com",
        }),
    ).toThrow(/handler OR webhookUrl/);
  });

  it("Tool throws with empty name", () => {
    expect(
      () => new Tool({ name: "", handler: async () => "x" }),
    ).toThrow(/name/);
  });

  it("tool() factory returns a Tool instance", () => {
    const t = tool({ name: "fn", handler: async () => "ok" });
    expect(t).toBeInstanceOf(Tool);
  });

  it("tool() factory also enforces exactly one of handler/webhookUrl", () => {
    expect(() => tool({ name: "fn" })).toThrow(/handler or webhookUrl/);
  });

  it("Guardrail with blockedTerms instantiates", () => {
    const g = new Guardrail({ name: "test", blockedTerms: ["bad"] });
    expect(g.name).toBe("test");
    expect(g.blockedTerms).toEqual(["bad"]);
    expect(g.replacement).toMatch(/I'm sorry/);
  });

  it("Guardrail with custom replacement overrides default", () => {
    const g = new Guardrail({ name: "t", blockedTerms: ["x"], replacement: "nope" });
    expect(g.replacement).toBe("nope");
  });

  it("Guardrail with custom check function", () => {
    const g = new Guardrail({ name: "t", check: () => true });
    expect(typeof g.check).toBe("function");
  });

  it("Guardrail throws with empty name", () => {
    expect(() => new Guardrail({ name: "" })).toThrow(/name/);
  });

  it("guardrail() factory returns a Guardrail instance", () => {
    const g = guardrail({ name: "g", blockedTerms: ["x"] });
    expect(g).toBeInstanceOf(Guardrail);
  });
});

// ---------------------------------------------------------------------------
// Flat re-exports from the package root
// ---------------------------------------------------------------------------

describe("flat re-exports from getpatter", () => {
  it("DeepgramSTT, WhisperSTT, CartesiaSTT, SonioxSTT, AssemblyAISTT are classes", () => {
    expect(typeof flat.DeepgramSTT).toBe("function");
    expect(typeof flat.WhisperSTT).toBe("function");
    expect(typeof flat.CartesiaSTT).toBe("function");
    expect(typeof flat.SonioxSTT).toBe("function");
    expect(typeof flat.AssemblyAISTT).toBe("function");
  });

  it("ElevenLabsTTS, OpenAITTS, CartesiaTTS, RimeTTS, LMNTTTS are classes", () => {
    expect(typeof flat.ElevenLabsTTS).toBe("function");
    expect(typeof flat.OpenAITTS).toBe("function");
    expect(typeof flat.CartesiaTTS).toBe("function");
    expect(typeof flat.RimeTTS).toBe("function");
    expect(typeof flat.LMNTTTS).toBe("function");
  });

  it("Twilio and Telnyx carriers are exposed", () => {
    expect(typeof flat.Twilio).toBe("function");
    expect(typeof flat.Telnyx).toBe("function");
    const t = new flat.Twilio({ accountSid: "ACx", authToken: "tok" });
    expect(t.kind).toBe("twilio");
  });

  it("OpenAIRealtime and ElevenLabsConvAI engines are exposed", () => {
    const r = new flat.OpenAIRealtime({ apiKey: "sk-x" });
    expect(r.kind).toBe("openai_realtime");
    const c = new flat.ElevenLabsConvAI({ apiKey: "el", agentId: "ag" });
    expect(c.kind).toBe("elevenlabs_convai");
  });

  it("CloudflareTunnel and StaticTunnel are exposed", () => {
    expect(new flat.CloudflareTunnel().kind).toBe("cloudflare");
    expect(new flat.StaticTunnel({ hostname: "x.com" }).kind).toBe("static");
  });

  it("Tool, Guardrail, tool, guardrail are exposed", () => {
    expect(flat.Tool).toBe(Tool);
    expect(flat.Guardrail).toBe(Guardrail);
    expect(flat.tool).toBe(tool);
    expect(flat.guardrail).toBe(guardrail);
  });

  it("new DeepgramSTT reads DEEPGRAM_API_KEY via flat export", () => {
    process.env.DEEPGRAM_API_KEY = "dg_flat";
    expect(new flat.DeepgramSTT()).toBeDefined();
  });
});
