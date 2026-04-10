/**
 * @file basic_inbound.ts
 * @description Minimal inbound voice agent using Patter SDK with OpenAI Realtime.
 *
 * A friendly restaurant reservation assistant that handles incoming calls.
 * Listens on port 8000 for Twilio webhooks and connects callers to the
 * OpenAI Realtime voice model.
 *
 * Prerequisites:
 *   - OPENAI_API_KEY, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN in .env
 *   - A Twilio phone number pointed at your webhook URL
 *
 * Usage:
 *   npx ts-node basic_inbound.ts
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
  systemPrompt: `You are a friendly reservation assistant for "La Cucina Bella",
an Italian restaurant. Help callers book tables, answer questions about the menu,
and provide directions. Be warm, concise, and professional. If the caller asks
about availability, suggest the next open slot. Always confirm the reservation
details before ending the call.`,
  voice: "nova",
  firstMessage:
    "Hello! Thank you for calling La Cucina Bella. How can I help you today?",
});

async function main(): Promise<void> {
  await phone.serve({ agent, port: 8000 });
  console.log("Inbound agent listening on port 8000");
}

main();
