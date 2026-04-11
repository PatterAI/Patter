/**
 * @file dynamic_variables.ts
 * @description Variable substitution in system prompts for personalized calls.
 *
 * Uses placeholder tokens like {customerName} and {accountNumber} in the
 * system prompt, then resolves them at call time via the variables config
 * and onCallStart overrides. This lets you reuse a single agent definition
 * across many callers with personalized context.
 *
 * Prerequisites:
 *   - OPENAI_API_KEY, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN in .env
 *
 * Usage:
 *   npx tsx dynamic_variables.ts
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

// Customer database keyed by phone number.
// Replace with a real database lookup in production.
const CUSTOMERS: Record<string, Record<string, string>> = {
  "+14155551234": {
    customerName: "Alice Chen",
    accountNumber: "AC-78901",
    planTier: "Enterprise",
  },
  "+14155555678": {
    customerName: "Bob Martinez",
    accountNumber: "AC-45678",
    planTier: "Starter",
  },
};

const agent = phone.agent({
  systemPrompt: `You are a personal account manager for {customerName}.
Their account number is {accountNumber} and their plan is {planTier}.
Greet them by name, answer billing questions, and offer relevant upgrades
based on their current plan. Be warm and concise.`,
  firstMessage: "Hi {customerName}! Thanks for calling. How can I help you today?",
  variables: {
    customerName: "Valued Customer",
    accountNumber: "unknown",
    planTier: "Free",
  },
});

async function onCallStart(data: Record<string, unknown>): Promise<Record<string, unknown> | void> {
  const caller = data.caller as string;
  const customer = CUSTOMERS[caller];
  if (customer) {
    console.log(`Known caller ${caller} → ${customer.customerName}`);
    return { variables: customer };
  }
  console.log(`Unknown caller ${caller} — using default variables`);
}

async function main(): Promise<void> {
  await phone.serve({ agent, port: 8000, onCallStart });
}

main();
