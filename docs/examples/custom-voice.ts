/**
 * Local mode with custom STT and TTS providers.
 *
 * Connects using:
 * - Twilio for telephony
 * - Deepgram Nova for speech-to-text
 * - ElevenLabs for text-to-speech
 *
 * No cloud backend required — runs entirely in your process.
 *
 * Usage:
 *   npm install getpatter
 *   # Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, DEEPGRAM_API_KEY, ELEVENLABS_API_KEY
 *   npx tsx custom-voice.ts
 */

import { Patter, Twilio, DeepgramSTT, ElevenLabsTTS, IncomingMessage } from "getpatter";

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
    carrier: new Twilio(),                              // TWILIO_* from env
    phoneNumber: PHONE_NUMBER,
  });

  const agent = phone.agent({
    stt: new DeepgramSTT(),                             // DEEPGRAM_API_KEY from env
    tts: new ElevenLabsTTS({ voiceId: "aria" }),        // ELEVENLABS_API_KEY from env
    systemPrompt: "You are a helpful customer service agent.",
    firstMessage: "Hi! Thanks for calling. How can I help?",
  });

  console.log(`Ready on ${PHONE_NUMBER}. Waiting for calls... (Ctrl+C to stop)`);

  try {
    await phone.serve({
      agent,
      port: 8000,
      onMessage,
      onCallStart: async (data) => {
        console.log(`Call started from ${data.caller}`);
      },
      onCallEnd: async (data) => {
        console.log(`Call ended after ${data.duration_seconds ?? 0}s`);
      },
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
