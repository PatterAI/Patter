/**
 * @file remote_webhook.ts
 * @description Route onMessage to a remote webhook or WebSocket endpoint.
 *
 * Instead of running LLM logic in-process, point onMessage at an external URL.
 * Patter POSTs user transcripts to your server and speaks whatever your
 * endpoint returns. This is ideal for:
 *   - Keeping LLM logic behind your own firewall
 *   - Sharing one message handler across multiple Patter instances
 *   - Environments where you cannot run custom code alongside Patter
 *
 * Webhook contract:
 *   POST https://api.yourcompany.com/patter/message
 *   Headers:  Content-Type: application/json
 *             X-Patter-Signature: <HMAC-SHA256 of body with your secret>
 *   Body:     { "transcript": "...", "callId": "...", "caller": "..." }
 *   Response: { "reply": "text to speak" }
 *
 * WebSocket alternative:
 *   Pass a "wss://..." URL instead. Patter opens a persistent socket and
 *   sends JSON frames with the same shape. Your server responds with
 *   { "reply": "..." } frames.
 *
 * Prerequisites:
 *   - TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER in .env
 *   - DEEPGRAM_API_KEY, ELEVENLABS_API_KEY in .env
 *   - WEBHOOK_URL pointing to this server (e.g. ngrok)
 *
 * Usage:
 *   npx ts-node remote_webhook.ts
 */

import { Patter, deepgram, elevenlabs } from "getpatter";
import dotenv from "dotenv";

dotenv.config();

const phone = new Patter({
  mode: "local",
  twilioSid: process.env.TWILIO_ACCOUNT_SID!,
  twilioToken: process.env.TWILIO_AUTH_TOKEN!,
  phoneNumber: process.env.TWILIO_PHONE_NUMBER!,
  webhookUrl: process.env.WEBHOOK_URL!,
});

const agent = phone.agent({
  systemPrompt: "You are an enterprise sales assistant.",
  provider: "pipeline",
  stt: deepgram({ apiKey: process.env.DEEPGRAM_API_KEY! }),
  tts: elevenlabs({ apiKey: process.env.ELEVENLABS_API_KEY!, voice: "rachel" }),
  firstMessage: "Hi there! I'd love to help you find the right plan.",
});

async function main(): Promise<void> {
  // Pass a URL string as onMessage — Patter handles HTTP POST / WebSocket
  // negotiation automatically. Your endpoint receives the transcript and
  // returns the reply text.
  await phone.serve({
    agent,
    port: 8000,
    onMessage: "https://api.yourcompany.com/patter/message",
    // WebSocket alternative:
    // onMessage: "wss://api.yourcompany.com/patter/ws",
    onCallStart: async (data) => {
      console.log(`Call started — ID: ${data.callId}`);
    },
    onCallEnd: async (data) => {
      console.log(`Call ended — duration: ${data.duration}s`);
    },
  });

  console.log("Remote webhook agent listening on port 8000");
}

main();
