/**
 * @file multi_agent.ts
 * @description Run multiple agents on different phone numbers.
 *
 * Spin up separate Patter instances — each with its own phone number, prompt,
 * and voice — on different ports. This pattern works well for:
 *   - Routing sales and support to different numbers
 *   - Running multilingual agents (English on one, Spanish on another)
 *   - A/B testing different prompts on live traffic
 *
 * Each instance is fully independent: its own Twilio number, webhook URL,
 * dashboard, and metrics.
 *
 * Prerequisites:
 *   - OPENAI_API_KEY in .env
 *   - TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN in .env
 *   - Two phone numbers: SALES_PHONE_NUMBER, SUPPORT_PHONE_NUMBER
 *   - Two webhook URLs: SALES_WEBHOOK_URL, SUPPORT_WEBHOOK_URL
 *
 * Usage:
 *   npx ts-node multi_agent.ts
 */

import { Patter } from "patter";
import dotenv from "dotenv";

dotenv.config();

// --- Sales agent ---
const salesPhone = new Patter({
  mode: "local",
  openaiKey: process.env.OPENAI_API_KEY!,
  twilioSid: process.env.TWILIO_ACCOUNT_SID!,
  twilioToken: process.env.TWILIO_AUTH_TOKEN!,
  phoneNumber: process.env.SALES_PHONE_NUMBER!,
  webhookUrl: process.env.SALES_WEBHOOK_URL!,
});

const salesAgent = salesPhone.agent({
  systemPrompt: `You are a persuasive but honest sales representative for Acme Corp.
Qualify leads, explain pricing tiers, and book demos. Never make promises
the product cannot keep.`,
  voice: "alloy",
  firstMessage: "Thanks for calling Acme sales! What can I help you explore today?",
});

// --- Support agent ---
const supportPhone = new Patter({
  mode: "local",
  openaiKey: process.env.OPENAI_API_KEY!,
  twilioSid: process.env.TWILIO_ACCOUNT_SID!,
  twilioToken: process.env.TWILIO_AUTH_TOKEN!,
  phoneNumber: process.env.SUPPORT_PHONE_NUMBER!,
  webhookUrl: process.env.SUPPORT_WEBHOOK_URL!,
});

const supportAgent = supportPhone.agent({
  systemPrompt: `You are a patient and thorough technical support agent for Acme Corp.
Troubleshoot issues step by step, escalate to a human when needed,
and always confirm the customer's problem is resolved before ending.`,
  voice: "nova",
  firstMessage: "Hello! You've reached Acme support. What issue are you experiencing?",
});

async function main(): Promise<void> {
  await Promise.all([
    salesPhone.serve({
      agent: salesAgent,
      port: 8000,
      dashboard: true,
      dashboardToken: process.env.SALES_DASHBOARD_TOKEN,
    }),
    supportPhone.serve({
      agent: supportAgent,
      port: 8001,
      dashboard: true,
      dashboardToken: process.env.SUPPORT_DASHBOARD_TOKEN,
    }),
  ]);

  console.log("Sales agent   -> port 8000");
  console.log("Support agent -> port 8001");
}

main();
