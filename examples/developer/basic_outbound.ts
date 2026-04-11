/**
 * @file basic_outbound.ts
 * @description Outbound voice call with answering machine detection (AMD).
 *
 * Places a single outbound call using Patter SDK. When AMD detects a voicemail,
 * the agent leaves a pre-configured message. Lifecycle callbacks log call start
 * and end events.
 *
 * Prerequisites:
 *   - OPENAI_API_KEY, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN in .env
 *   - A verified Twilio phone number
 *
 * Usage:
 *   npx ts-node basic_outbound.ts
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

const agent = phone.agent({
  systemPrompt: `You are a polite appointment reminder assistant for Dr. Smith's
dental office. Remind the patient of their upcoming appointment, confirm the date
and time, and ask if they need to reschedule. Keep the call brief and friendly.`,
  voice: "nova",
  firstMessage:
    "Hi! This is a friendly reminder from Dr. Smith's dental office about your upcoming appointment.",
});

async function main(): Promise<void> {
  const call = await phone.call({
    to: "+15551234567",
    agent,
    machineDetection: true,
    voicemailMessage:
      "Hi, this is Dr. Smith's office calling to remind you of your upcoming appointment. Please call us back at your earliest convenience. Thank you!",
    onCallStart: (event) => {
      console.log(`Call started — SID: ${event.callSid}`);
    },
    onCallEnd: (event) => {
      console.log(
        `Call ended — duration: ${event.duration}s, status: ${event.status}`
      );
    },
  });

  console.log(`Outbound call placed — SID: ${call.sid}`);
}

main();
