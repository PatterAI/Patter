/**
 * Basic inbound call handler.
 *
 * The AI answers incoming calls with OpenAI Realtime as the default engine.
 * Swap to ElevenLabs ConvAI or pipeline mode (stt/tts) for full control.
 *
 * Usage:
 *   npm install getpatter
 *   # Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, OPENAI_API_KEY in the env
 *   npx tsx basic-inbound.ts
 */

import { Patter, Twilio, OpenAIRealtime } from "getpatter";

const PHONE_NUMBER = process.env.PHONE_NUMBER ?? "+15550001234";

async function main(): Promise<void> {
  const phone = new Patter({
    carrier: new Twilio(),              // reads TWILIO_ACCOUNT_SID + TWILIO_AUTH_TOKEN
    phoneNumber: PHONE_NUMBER,
  });

  const agent = phone.agent({
    engine: new OpenAIRealtime(),       // reads OPENAI_API_KEY
    systemPrompt:
      "You are the receptionist for Acme Corp. Help callers with hours, " +
      "support questions, and simple account changes. Keep replies short.",
    firstMessage: "Hi! Thanks for calling Acme Corp. How can I help?",
  });

  console.log(`Ready on ${PHONE_NUMBER}. Waiting for calls... (Ctrl+C to stop)`);

  await phone.serve({
    agent,
    tunnel: true,                       // start a Cloudflare Quick Tunnel for dev
    onCallStart: async (data) => {
      console.log(`Incoming call from ${data.caller ?? "unknown"}`);
    },
    onCallEnd: async (data) => {
      console.log(`Call ended after ${data.duration_seconds ?? 0}s`);
    },
    onTranscript: async (event) => {
      console.log(`[${event.role}]: ${event.text}`);
    },
  });
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
