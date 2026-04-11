/**
 * @file cost_tracking.ts
 * @description Real-time and per-call cost tracking for voice agents.
 *
 * Demonstrates how to configure per-provider pricing, monitor costs
 * in real time via onMetrics, and generate a cost breakdown report
 * when each call ends. Essential for startups watching unit economics.
 *
 * Prerequisites:
 *   - OPENAI_API_KEY, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN in .env
 *   - DEEPGRAM_API_KEY in .env
 *   - A Twilio phone number pointed at your webhook URL
 *
 * Usage:
 *   npx ts-node cost_tracking.ts
 */

import { Patter } from "getpatter";
import dotenv from "dotenv";

dotenv.config();

const phone = new Patter({
  mode: "local",
  openaiKey: process.env.OPENAI_API_KEY!,
  twilioSid: process.env.TWILIO_ACCOUNT_SID!,
  twilioToken: process.env.TWILIO_AUTH_TOKEN!,
  phoneNumber: process.env.TWILIO_PHONE_NUMBER!,
  webhookUrl: process.env.WEBHOOK_URL!,
  pricing: {
    deepgram: { price: 0.005 },
  },
});

const agent = phone.agent({
  provider: "pipeline",
  stt: Patter.deepgram({
    apiKey: process.env.DEEPGRAM_API_KEY!,
    language: "en",
  }),
  systemPrompt: `You are a helpful billing support agent. Answer questions about
invoices, payment methods, and account balances. Be clear and accurate.`,
  firstMessage: "Hello! I'm your billing assistant. What can I help you with?",
});

agent.onMetrics((metrics) => {
  console.log(`[metrics] STT: $${metrics.cost.stt.toFixed(4)} | LLM: $${metrics.cost.llm.toFixed(4)} | TTS: $${metrics.cost.tts.toFixed(4)}`);
});

agent.onCallEnd((summary) => {
  const { cost } = summary.metrics;
  const total = cost.stt + cost.llm + cost.tts + cost.telephony;

  console.log("\n--- Call Cost Report ---");
  console.log(`Duration:   ${summary.metrics.durationSeconds}s`);
  console.log(`STT:        $${cost.stt.toFixed(4)}`);
  console.log(`LLM:        $${cost.llm.toFixed(4)}`);
  console.log(`TTS:        $${cost.tts.toFixed(4)}`);
  console.log(`Telephony:  $${cost.telephony.toFixed(4)}`);
  console.log(`Total:      $${total.toFixed(4)}`);
  console.log("------------------------\n");
});

async function main(): Promise<void> {
  await phone.serve({ agent, port: 8000 });
  console.log("Cost-tracked agent listening on port 8000");
}

main();
