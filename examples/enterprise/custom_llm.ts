/**
 * @file custom_llm.ts
 * @description Bring your own LLM with Patter's pipeline mode.
 *
 * Patter handles speech-to-text and text-to-speech while you control the
 * "brain" — any LLM, fine-tuned model, or deterministic logic. This example
 * uses Anthropic Claude via a simple fetch call, but the pattern works with
 * any HTTP-accessible model (OpenAI, Mistral, Llama, etc.).
 *
 * How it works:
 *   1. Caller speaks -> Patter transcribes via Deepgram STT
 *   2. Transcript arrives in your onMessage handler
 *   3. You call your own LLM and return the response string
 *   4. Patter speaks the response via ElevenLabs TTS
 *
 * Prerequisites:
 *   - ANTHROPIC_API_KEY in .env (or whichever LLM you use)
 *   - TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER in .env
 *   - DEEPGRAM_API_KEY, ELEVENLABS_API_KEY in .env
 *   - WEBHOOK_URL pointing to this server (e.g. ngrok)
 *
 * Usage:
 *   npx ts-node custom_llm.ts
 */

import { Patter, deepgram, elevenlabs } from "patter";
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
  systemPrompt: "You are a helpful enterprise support agent.",
  provider: "pipeline",
  stt: deepgram({ apiKey: process.env.DEEPGRAM_API_KEY! }),
  tts: elevenlabs({ apiKey: process.env.ELEVENLABS_API_KEY!, voice: "rachel" }),
  firstMessage: "Hello, how can I assist you today?",
});

/** Call your own LLM — replace with any model or routing logic. */
async function callAnthropic(userMessage: string): Promise<string> {
  const response = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: {
      "x-api-key": process.env.ANTHROPIC_API_KEY!,
      "anthropic-version": "2023-06-01",
      "content-type": "application/json",
    },
    body: JSON.stringify({
      model: "claude-sonnet-4-20250514",
      max_tokens: 256,
      system: agent.systemPrompt,
      messages: [{ role: "user", content: userMessage }],
    }),
  });

  const body = (await response.json()) as Record<string, unknown>;
  const content = body.content as Array<{ text: string }>;
  return content[0].text;
}

async function main(): Promise<void> {
  await phone.serve({
    agent,
    port: 8000,
    onMessage: async (data) => {
      const transcript = data.transcript as string;
      console.log(`User said: ${transcript}`);

      const reply = await callAnthropic(transcript);
      console.log(`LLM replied: ${reply}`);
      return reply;
    },
  });

  console.log("Custom LLM agent listening on port 8000");
}

main();
