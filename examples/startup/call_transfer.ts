/**
 * @file call_transfer.ts
 * @description Call control with live transfer and hangup.
 *
 * Shows how to use the callControl parameter in onMessage to transfer
 * callers to a human agent or gracefully end the call based on
 * conversation context. The agent triages incoming calls and escalates
 * when it cannot resolve the issue.
 *
 * Prerequisites:
 *   - OPENAI_API_KEY, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN in .env
 *   - A Twilio phone number pointed at your webhook URL
 *
 * Usage:
 *   npx ts-node call_transfer.ts
 */

import { Patter } from "patter";
import dotenv from "dotenv";

dotenv.config();

const HUMAN_AGENT_NUMBER = process.env.TRANSFER_NUMBER ?? "+15551234567";

const phone = new Patter({
  mode: "local",
  openaiKey: process.env.OPENAI_API_KEY!,
  twilioSid: process.env.TWILIO_ACCOUNT_SID!,
  twilioToken: process.env.TWILIO_AUTH_TOKEN!,
  phoneNumber: process.env.TWILIO_PHONE_NUMBER!,
  webhookUrl: process.env.WEBHOOK_URL!,
});

const agent = phone.agent({
  systemPrompt: `You are a triage assistant for an insurance company. Help callers
with simple questions about their policy. If the caller needs to file a claim
or requests to speak with a human, let them know you are transferring them.
If the caller says "goodbye" or the issue is resolved, end the call politely.`,
  firstMessage: "Hello! Welcome to SafeGuard Insurance. How can I assist you today?",
});

agent.onMessage((message, callControl) => {
  console.log(`[${message.role}] ${message.content}`);

  if (callControl.ended) {
    console.log("Call has already ended, skipping control logic.");
    return;
  }

  const text = message.content.toLowerCase();

  if (text.includes("transferring you") || text.includes("connect you to a human")) {
    console.log(`Transferring call to ${HUMAN_AGENT_NUMBER}...`);
    callControl.transfer(HUMAN_AGENT_NUMBER);
    return;
  }

  if (text.includes("goodbye") || text.includes("have a great day")) {
    console.log("Ending call gracefully.");
    callControl.hangup();
  }
});

async function main(): Promise<void> {
  await phone.serve({ agent, port: 8000 });
  console.log("Call-transfer agent listening on port 8000");
}

main();
