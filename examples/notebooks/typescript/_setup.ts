/**
 * Shared helpers for every notebook in examples/notebooks/typescript/.
 *
 * Mirror of python/_setup.py. Keep field names, behaviour, and exit codes
 * aligned with the Python module — the parity check script enforces shape.
 */

import * as fs from "node:fs";
import * as path from "node:path";
import * as dotenv from "dotenv";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const NOTEBOOKS_DIR = path.dirname(HERE);
export const FIXTURES = path.join(NOTEBOOKS_DIR, "fixtures");

export interface NotebookEnv {
  readonly openaiKey: string;
  readonly anthropicKey: string;
  readonly googleKey: string;
  readonly groqKey: string;
  readonly cerebrasKey: string;
  readonly deepgramKey: string;
  readonly assemblyaiKey: string;
  readonly sonioxKey: string;
  readonly speechmaticsKey: string;
  readonly cartesiaKey: string;
  readonly elevenlabsKey: string;
  readonly elevenlabsVoiceId: string;
  readonly elevenlabsAgentId: string;
  readonly lmntKey: string;
  readonly rimeKey: string;
  readonly ultravoxKey: string;
  readonly twilioSid: string;
  readonly twilioToken: string;
  readonly twilioNumber: string;
  readonly telnyxKey: string;
  readonly telnyxConnectionId: string;
  readonly telnyxNumber: string;
  readonly telnyxPublicKey: string;
  readonly targetNumber: string;
  readonly ngrokToken: string;
  readonly publicWebhookUrl: string;
  readonly patterVersion: string;
  readonly enableLiveCalls: boolean;
  readonly maxCallSeconds: number;
  readonly maxCostUsd: number;
}

const get = (n: string, d = ""): string => (process.env[n] ?? d).trim();

export function load(opts: { envFile?: string } = {}): NotebookEnv {
  const envFile = opts.envFile ?? path.join(NOTEBOOKS_DIR, ".env");
  if (fs.existsSync(envFile)) {
    dotenv.config({ path: envFile, override: false });
  }
  return Object.freeze<NotebookEnv>({
    openaiKey: get("OPENAI_API_KEY"),
    anthropicKey: get("ANTHROPIC_API_KEY"),
    googleKey: get("GOOGLE_API_KEY"),
    groqKey: get("GROQ_API_KEY"),
    cerebrasKey: get("CEREBRAS_API_KEY"),
    deepgramKey: get("DEEPGRAM_API_KEY"),
    assemblyaiKey: get("ASSEMBLYAI_API_KEY"),
    sonioxKey: get("SONIOX_API_KEY"),
    speechmaticsKey: get("SPEECHMATICS_API_KEY"),
    cartesiaKey: get("CARTESIA_API_KEY"),
    elevenlabsKey: get("ELEVENLABS_API_KEY"),
    elevenlabsVoiceId: get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"),
    elevenlabsAgentId: get("ELEVENLABS_AGENT_ID"),
    lmntKey: get("LMNT_API_KEY"),
    rimeKey: get("RIME_API_KEY"),
    ultravoxKey: get("ULTRAVOX_API_KEY"),
    twilioSid: get("TWILIO_ACCOUNT_SID"),
    twilioToken: get("TWILIO_AUTH_TOKEN"),
    twilioNumber: get("TWILIO_PHONE_NUMBER"),
    telnyxKey: get("TELNYX_API_KEY"),
    telnyxConnectionId: get("TELNYX_CONNECTION_ID"),
    telnyxNumber: get("TELNYX_PHONE_NUMBER"),
    telnyxPublicKey: get("TELNYX_PUBLIC_KEY"),
    targetNumber: get("TARGET_PHONE_NUMBER"),
    ngrokToken: get("NGROK_AUTHTOKEN"),
    publicWebhookUrl: get("PUBLIC_WEBHOOK_URL"),
    patterVersion: get("PATTER_VERSION", "0.5.2"),
    enableLiveCalls: get("ENABLE_LIVE_CALLS", "0") === "1",
    maxCallSeconds: parseInt(get("NOTEBOOK_MAX_CALL_SECONDS", "90"), 10),
    maxCostUsd: parseFloat(get("NOTEBOOK_MAX_COST_USD", "0.25")),
  });
}

const KEY_FIELD_MAP: Record<string, keyof NotebookEnv> = {
  OPENAI_API_KEY: "openaiKey",
  ANTHROPIC_API_KEY: "anthropicKey",
  GOOGLE_API_KEY: "googleKey",
  GROQ_API_KEY: "groqKey",
  CEREBRAS_API_KEY: "cerebrasKey",
  DEEPGRAM_API_KEY: "deepgramKey",
  ASSEMBLYAI_API_KEY: "assemblyaiKey",
  SONIOX_API_KEY: "sonioxKey",
  SPEECHMATICS_API_KEY: "speechmaticsKey",
  CARTESIA_API_KEY: "cartesiaKey",
  ELEVENLABS_API_KEY: "elevenlabsKey",
  ELEVENLABS_AGENT_ID: "elevenlabsAgentId",
  LMNT_API_KEY: "lmntKey",
  RIME_API_KEY: "rimeKey",
  ULTRAVOX_API_KEY: "ultravoxKey",
  TWILIO_ACCOUNT_SID: "twilioSid",
  TWILIO_AUTH_TOKEN: "twilioToken",
  TWILIO_PHONE_NUMBER: "twilioNumber",
  TELNYX_API_KEY: "telnyxKey",
  TELNYX_CONNECTION_ID: "telnyxConnectionId",
  TELNYX_PHONE_NUMBER: "telnyxNumber",
  TELNYX_PUBLIC_KEY: "telnyxPublicKey",
  TARGET_PHONE_NUMBER: "targetNumber",
  NGROK_AUTHTOKEN: "ngrokToken",
  PUBLIC_WEBHOOK_URL: "publicWebhookUrl",
};

export class NotebookSkip extends Error {
  constructor(reason: string) {
    super(reason);
    this.name = "NotebookSkip";
  }
}

export function hasKey(env: NotebookEnv, name: string): boolean {
  const fieldName = KEY_FIELD_MAP[name];
  const v = fieldName ? (env as any)[fieldName] : process.env[name] ?? "";
  return Boolean(v);
}

export function skip(reason: string): never {
  throw new NotebookSkip(reason);
}

export function skipSection(reason: string): never {
  throw new NotebookSkip(`[section skipped] ${reason}`);
}

export function printKeyMatrix(env: NotebookEnv, required: string[]): void {
  console.log("Key matrix:");
  for (const name of required) {
    const marker = hasKey(env, name) ? "✅" : "⚪";
    console.log(`  ${marker} ${name}`);
  }
}

const REAL_PHONE = /\+1[2-9]\d{9}/g;
const REAL_TWILIO_SID = /\bAC[0-9a-f]{32}\b/g;

function assertRedacted(body: string, source: string): void {
  for (const m of body.match(REAL_PHONE) ?? []) {
    if (m !== "+15555550100") {
      throw new Error(`${source} contains non-placeholder phone ${m}`);
    }
  }
  for (const m of body.match(REAL_TWILIO_SID) ?? []) {
    if (!(m.startsWith("ACtest") || m.slice(2).split("").every((c) => c === "0"))) {
      throw new Error(`${source} contains non-placeholder Twilio SID ${m}`);
    }
  }
}

export function loadFixture(relPath: string): Buffer {
  const p = path.join(FIXTURES, relPath);
  if (!fs.existsSync(p)) throw new Error(`fixture not found: ${p}`);
  const data = fs.readFileSync(p);
  if (p.endsWith(".json")) assertRedacted(data.toString("utf-8"), p);
  return data;
}

export async function cell<T>(
  name: string,
  opts: { tier: number; required?: string[]; env?: NotebookEnv },
  body: () => Promise<T> | T,
): Promise<void> {
  const env = opts.env ?? load();
  const started = Date.now();

  if (opts.tier === 4 && !env.enableLiveCalls) {
    console.log(`⚪ [${name}] skipped — set ENABLE_LIVE_CALLS=1 to enable T4 live calls.`);
    return;
  }

  const missing = (opts.required ?? []).filter((k) => !hasKey(env, k));
  if (missing.length) {
    console.log(`⚪ [${name}] skipped — missing env: ${missing.join(", ")}`);
    return;
  }

  console.log(`▶ [${name}] tier=${opts.tier}`);
  try {
    await body();
  } catch (exc) {
    if (exc instanceof NotebookSkip) {
      console.log(`⚪ [${name}] ${exc.message}`);
      return;
    }
    const e = exc as Error;
    const elapsed = (Date.now() - started) / 1000;
    console.log(`❌ [${name}] failed after ${elapsed.toFixed(2)}s: ${e.name}: ${e.message}`);
    if (e.stack) console.log(e.stack.split("\n").slice(0, 6).join("\n"));
    return;
  }
  const elapsed = (Date.now() - started) / 1000;
  console.log(`✅ [${name}] ${elapsed.toFixed(2)}s`);
}

interface STTLike {
  connect(): Promise<void>;
  sendAudio(chunk: Buffer): Promise<void>;
  close(): Promise<void>;
  receiveTranscripts(): AsyncIterable<string>;
}

export async function runStt(stt: STTLike, audio: Buffer, chunkSize = 3200): Promise<string> {
  await stt.connect();
  try {
    for (let i = 0; i < audio.length; i += chunkSize) {
      await stt.sendAudio(audio.subarray(i, i + chunkSize));
    }
    const out: string[] = [];
    for await (const piece of stt.receiveTranscripts()) out.push(piece);
    return out.join("");
  } finally {
    await stt.close();
  }
}

interface TTSLike {
  synthesize(text: string): AsyncIterable<Buffer>;
}

export async function runTts(tts: TTSLike, text: string): Promise<Buffer> {
  const chunks: Buffer[] = [];
  for await (const chunk of tts.synthesize(text)) chunks.push(chunk);
  return Buffer.concat(chunks);
}

export async function hangupLeftoverCalls(env: NotebookEnv): Promise<void> {
  if (!(env.twilioSid && env.twilioToken && env.twilioNumber)) return;
  let twilio: any;
  try {
    const mod = await import("twilio");
    twilio = (mod.default ?? mod)(env.twilioSid, env.twilioToken);
  } catch {
    console.log("⚪ twilio package not installed — skipping hangup sweep");
    return;
  }
  try {
    const calls = await twilio.calls.list({ from: env.twilioNumber, status: "in-progress", limit: 5 });
    for (const c of calls) {
      try {
        await twilio.calls(c.sid).update({ status: "completed" });
        console.log(`🔚 hung up stale call ${c.sid}`);
      } catch (exc) {
        console.log(`⚠ could not hang up ${c.sid}: ${(exc as Error).message}`);
      }
    }
  } catch (exc) {
    console.log(`⚠ Twilio sweep failed: ${(exc as Error).message}`);
  }
}
