/**
 * Self-hosted mode with custom STT and TTS providers.
 *
 * Connects to your own Patter backend with:
 * - Telnyx for telephony
 * - Deepgram Nova for speech-to-text
 * - ElevenLabs for text-to-speech
 *
 * Requires a running Patter backend (see docs/self-hosting.md).
 *
 * Usage:
 *   npm install getpatter
 *   npx ts-node custom-voice.ts
 */

import { Patter, IncomingMessage } from "getpatter";

// Configuration — use environment variables in production
const PATTER_API_KEY = process.env.PATTER_API_KEY ?? "pt_your_api_key_here";
const TELNYX_API_KEY = process.env.TELNYX_API_KEY ?? "KEY4...";
const DEEPGRAM_API_KEY = process.env.DEEPGRAM_API_KEY ?? "dg_...";
const ELEVENLABS_API_KEY = process.env.ELEVENLABS_API_KEY ?? "el_...";
const PHONE_NUMBER = process.env.PHONE_NUMBER ?? "+14155550000"; // E.164 format

const BACKEND_WS = process.env.PATTER_BACKEND_WS ?? "ws://localhost:8000";
const BACKEND_REST = process.env.PATTER_BACKEND_REST ?? "http://localhost:8000";

const phone = new Patter({
  apiKey: PATTER_API_KEY,
  backendUrl: BACKEND_WS,
  restUrl: BACKEND_REST,
});

async function onMessage(msg: IncomingMessage): Promise<string> {
  console.log(`[${msg.callId}] ${msg.caller}: "${msg.text}"`);

  const text = msg.text.toLowerCase();

  if (text.includes("hours") || text.includes("open")) {
    return "We are open Monday through Friday from 9 to 5.";
  }

  if (text.includes("price") || text.includes("cost")) {
    return "Our pricing starts at $29 per month. Would you like to know more?";
  }

  if (text.includes("bye") || text.includes("goodbye") || text.includes("thanks")) {
    return "Thank you for calling. Have a wonderful day!";
  }

  return "How can I help you today?";
}

async function main(): Promise<void> {
  console.log(`Connecting to ${BACKEND_WS}...`);

  await phone.connect({
    onMessage,
    onCallStart: async (data) => {
      console.log(`Call started from ${data.caller} (mode: ${data.mode})`);
    },
    onCallEnd: async (data) => {
      console.log(`Call ended after ${data.duration_seconds ?? 0}s`);
    },
    // Self-hosted: pass provider and voice config
    provider: "telnyx",
    providerKey: TELNYX_API_KEY,
    number: PHONE_NUMBER,
    country: "US",
    stt: Patter.deepgram({ apiKey: DEEPGRAM_API_KEY, language: "en" }),
    tts: Patter.elevenlabs({ apiKey: ELEVENLABS_API_KEY, voice: "aria" }),
  });

  console.log(`Ready on ${PHONE_NUMBER}. Waiting for calls... (Ctrl+C to stop)`);

  await new Promise<never>(() => {});
}

process.on("SIGINT", async () => {
  console.log("\nShutting down...");
  await phone.disconnect();
  process.exit(0);
});

main().catch(console.error);
