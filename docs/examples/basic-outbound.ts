/**
 * Basic outbound call example.
 *
 * The AI places a call to a destination and confirms an appointment.
 *
 * Usage:
 *   npm install getpatter
 *   # Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, OPENAI_API_KEY in the env
 *   npx tsx basic-outbound.ts
 */

import { Patter, Twilio, OpenAIRealtime } from "getpatter";

const PHONE_NUMBER = process.env.PHONE_NUMBER ?? "+15550001234";
const DESTINATION = process.env.DESTINATION ?? "+14155551234";

async function main(): Promise<void> {
  const phone = new Patter({
    carrier: new Twilio(),              // TWILIO_* from env
    phoneNumber: PHONE_NUMBER,
  });

  const agent = phone.agent({
    engine: new OpenAIRealtime(),       // OPENAI_API_KEY from env
    systemPrompt:
      "You are calling to confirm an appointment scheduled for tomorrow at 3 PM. " +
      "Ask the callee to confirm. If they say yes, thank them and hang up. " +
      "If they say no, apologise and offer to reschedule.",
    firstMessage:
      "Hi! This is an automated reminder about your appointment tomorrow at 3 PM. " +
      "Can you confirm you'll make it?",
  });

  // Start the server in the background, then place the call
  const serveTask = phone.serve({
    agent,
    tunnel: true,
    onCallStart: async (data) => console.log(`Call connected (call_id=${data.call_id})`),
    onCallEnd: async (data) => console.log(`Call ended after ${data.duration_seconds ?? 0}s`),
  });
  await new Promise((r) => setTimeout(r, 1000));      // wait for the tunnel to come up

  console.log(`Calling ${DESTINATION}...`);
  await phone.call({
    to: DESTINATION,
    agent,
    machineDetection: true,
    voicemailMessage:
      "Hi, this is a reminder that your appointment is tomorrow at 3 PM. " +
      "Please call back if you need to reschedule.",
  });

  await serveTask;
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
