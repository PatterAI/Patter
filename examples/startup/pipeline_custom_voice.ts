/**
 * @file pipeline_custom_voice.ts
 * @description Pipeline mode voice agent with custom STT/TTS providers.
 *
 * Combines Deepgram for speech-to-text and ElevenLabs for text-to-speech
 * in pipeline mode, giving startups full control over the voice stack
 * while keeping orchestration simple.
 *
 * Prerequisites:
 *   - OPENAI_API_KEY, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN in .env
 *   - DEEPGRAM_API_KEY, ELEVENLABS_API_KEY in .env
 *   - A Twilio phone number pointed at your webhook URL
 *
 * Usage:
 *   npx ts-node pipeline_custom_voice.ts
 */

import { Patter } from "patter";
import dotenv from "dotenv";

dotenv.config();

const phone = new Patter({
  mode: "local",
  openaiKey: process.env.OPENAI_API_KEY!,
  twilioSid: process.env.TWILIO_ACCOUNT_SID!,
  twilioToken: process.env.TWILIO_AUTH_TOKEN!,
  phoneNumber: process.env.TWILIO_PHONE_NUMBER!,
  webhookUrl: process.env.WEBHOOK_URL!,
});

const agent = phone.agent({
  provider: "pipeline",
  stt: Patter.deepgram({
    apiKey: process.env.DEEPGRAM_API_KEY!,
    language: "en",
  }),
  tts: Patter.elevenlabs({
    apiKey: process.env.ELEVENLABS_API_KEY!,
    voice: "rachel",
  }),
  systemPrompt: `You are a friendly scheduling assistant for a dental clinic.
Help callers book, reschedule, or cancel appointments. Confirm all details
before finalizing. Keep responses concise and professional.`,
  firstMessage: "Hi there! Thanks for calling Bright Smile Dental. How can I help you today?",
});

agent.onMessage((message) => {
  console.log(`[${message.role}] ${message.content}`);
});

async function main(): Promise<void> {
  await phone.serve({ agent, port: 8000, recording: true });
  console.log("Pipeline agent with Deepgram + ElevenLabs listening on port 8000");
}

main();
