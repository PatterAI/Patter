/**
 * Basic outbound call example.
 *
 * The AI places a call to a phone number and holds a conversation.
 * Replace the API key and destination number before running.
 *
 * Usage:
 *   npm install getpatter
 *   npx ts-node basic-outbound.ts
 */

import { Patter, IncomingMessage } from "getpatter";

const DESTINATION = "+14155551234"; // Replace with a real number
const API_KEY = "pt_your_api_key_here";

const phone = new Patter({ apiKey: API_KEY });

async function onMessage(msg: IncomingMessage): Promise<string> {
  console.log(`Callee said: "${msg.text}"`);

  const text = msg.text.toLowerCase();

  if (text.includes("yes") || text.includes("confirm") || text.includes("sure")) {
    return "Perfect. Your appointment is confirmed. We will see you then. Goodbye!";
  }

  if (text.includes("no") || text.includes("cancel")) {
    return "No problem. I have cancelled your appointment. Have a good day. Goodbye!";
  }

  if (text.includes("when") || text.includes("time")) {
    return "Your appointment is scheduled for tomorrow at 3 PM. Can you confirm you will make it?";
  }

  return "I did not catch that. Could you say yes to confirm or no to cancel your appointment?";
}

async function main(): Promise<void> {
  console.log("Connecting to Patter...");

  await phone.connect({
    onMessage,
    onCallStart: async (data) => {
      console.log(`Call connected (call ID: ${data.call_id ?? "unknown"})`);
    },
    onCallEnd: async (data) => {
      const duration = data.duration_seconds ?? 0;
      console.log(`Call ended after ${duration} seconds`);
    },
  });

  console.log(`Calling ${DESTINATION}...`);

  await phone.call({
    to: DESTINATION,
    firstMessage:
      "Hi! This is an automated reminder about your appointment tomorrow at 3 PM. " +
      "Can you confirm you will make it?",
  });

  console.log("Call placed. Waiting for it to complete... (Ctrl+C to stop)");

  await new Promise<never>(() => {});
}

process.on("SIGINT", async () => {
  console.log("\nShutting down...");
  await phone.disconnect();
  process.exit(0);
});

main().catch(console.error);
