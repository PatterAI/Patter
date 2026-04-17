/**
 * Local mode with custom STT and TTS providers.
 *
 * Connects using local mode with:
 * - Twilio for telephony
 * - Deepgram Nova for speech-to-text
 * - ElevenLabs for text-to-speech
 *
 * No cloud backend required — runs entirely in your process.
 *
 * Usage:
 *   npm install getpatter
 *   npx ts-node custom-voice.ts
 */

import { Patter, IncomingMessage } from "getpatter";

// Configuration — use environment variables in production
const TWILIO_SID = process.env.TWILIO_ACCOUNT_SID ?? "AC...";
const TWILIO_TOKEN = process.env.TWILIO_AUTH_TOKEN ?? "your_auth_token";
const OPENAI_KEY = process.env.OPENAI_API_KEY ?? "sk-...";
const DEEPGRAM_API_KEY = process.env.DEEPGRAM_API_KEY ?? "dg_...";
const ELEVENLABS_API_KEY = process.env.ELEVENLABS_API_KEY ?? "el_...";
const PHONE_NUMBER = process.env.TWILIO_PHONE_NUMBER ?? "+14155550000"; // E.164 format

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
  const phone = new Patter({
    twilioSid: TWILIO_SID,
    twilioToken: TWILIO_TOKEN,
    openaiKey: OPENAI_KEY,
    phoneNumber: PHONE_NUMBER,
  });

  const agent = phone.agent({
    systemPrompt: "You are a helpful customer service agent.",
    voice: "alloy",
    firstMessage: "Hi! Thanks for calling. How can I help?",
  });

  console.log(`Ready on ${PHONE_NUMBER}. Waiting for calls... (Ctrl+C to stop)`);

  try {
    await phone.serve({
      agent,
      port: 8000,
      onCallStart: async (data) => {
        console.log(`Call started from ${data.caller}`);
      },
      onCallEnd: async (data) => {
        console.log(`Call ended after ${data.duration_seconds ?? 0}s`);
      },
      // Use custom STT and TTS providers
      stt: Patter.deepgram({ apiKey: DEEPGRAM_API_KEY, language: "en" }),
      tts: Patter.elevenlabs({ apiKey: ELEVENLABS_API_KEY, voice: "aria" }),
    });
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ERR_ASSERTION") {
      console.log("\nShutting down...");
    } else {
      throw error;
    }
  }
}

process.on("SIGINT", async () => {
  console.log("\nShutting down...");
  process.exit(0);
});

main().catch(console.error);
