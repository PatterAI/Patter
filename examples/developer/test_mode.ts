/**
 * @file test_mode.ts
 * @description Test mode for local development without telephony.
 *
 * Runs the agent in an interactive terminal session. No Twilio credentials
 * are required — type messages directly and see agent responses in real time.
 *
 * Special commands in test mode:
 *   /quit     — end the session
 *   /transfer — simulate a call transfer
 *   /hangup   — simulate the caller hanging up
 *   /history  — print the full conversation transcript
 *
 * Prerequisites:
 *   - OPENAI_API_KEY in .env
 *
 * Usage:
 *   npx ts-node test_mode.ts
 */

import { Patter } from "getpatter";
import dotenv from "dotenv";

dotenv.config();

const phone = new Patter({
  mode: "local",
  openaiKey: process.env.OPENAI_API_KEY!,
});

const agent = phone.agent({
  systemPrompt: `You are a helpful customer support agent for an e-commerce store.
Assist customers with order status, returns, and product questions. Be concise
and empathetic.`,
  voice: "nova",
  firstMessage: "Hi there! How can I help you with your order today?",
});

async function main(): Promise<void> {
  await phone.test(agent, {
    onMessage: (message) => {
      console.log(`[${message.role}]: ${message.content}`);
    },
    onCallStart: () => {
      console.log("--- Test session started ---");
    },
    onCallEnd: (event) => {
      console.log(`--- Test session ended (${event.duration}s) ---`);
    },
  });
}

main();
