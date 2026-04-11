/**
 * @file guardrails.ts
 * @description Voice agent with output guardrails.
 *
 * Demonstrates two guardrail types:
 *   1. Blocked-terms guardrail — prevents the agent from mentioning competitors.
 *   2. Custom-check guardrail — strips pricing information from responses.
 *
 * Guardrails run on every agent response before it reaches the caller, ensuring
 * compliance without modifying the system prompt.
 *
 * Prerequisites:
 *   - OPENAI_API_KEY, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN in .env
 *
 * Usage:
 *   npx ts-node guardrails.ts
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
});

const noCompetitors = Patter.guardrail({
  name: "no-competitors",
  blockedTerms: ["CompetitorA", "CompetitorB", "RivalCorp"],
});

const noPricing = Patter.guardrail({
  name: "no-pricing",
  check: (text: string) => !text.includes("$"),
  replacement:
    "I'm not able to share specific pricing over the phone. I can email you a detailed quote — would that work?",
});

const agent = phone.agent({
  systemPrompt: `You are a sales assistant for Acme Corp. Answer product questions,
highlight features, and help callers understand how Acme solutions fit their needs.
Never mention competitor products or share specific dollar amounts on the call.`,
  voice: "nova",
  firstMessage:
    "Welcome to Acme Corp! I'd love to help you find the right solution. What are you looking for?",
  guardrails: [noCompetitors, noPricing],
});

async function main(): Promise<void> {
  await phone.serve({ agent, port: 8000 });
  console.log("Guardrails agent listening on port 8000");
}

main();
