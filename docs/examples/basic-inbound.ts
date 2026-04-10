/**
 * Basic inbound call handler.
 *
 * The AI answers incoming calls and responds to what the caller says.
 * Replace the API key with your own from https://api.patter.dev.
 *
 * Usage:
 *   npm install patter
 *   npx ts-node basic-inbound.ts
 */

import { Patter, IncomingMessage } from "patter";

const phone = new Patter({ apiKey: "pt_your_api_key_here" });

async function onMessage(msg: IncomingMessage): Promise<string> {
  console.log(`Caller said: "${msg.text}"`);

  const text = msg.text.toLowerCase();

  if (text.includes("hours") || text.includes("open")) {
    return "We are open Monday through Friday, 9 AM to 6 PM Eastern time.";
  }

  if (text.includes("help") || text.includes("support")) {
    return "I can help you with billing, technical questions, or account changes. What do you need?";
  }

  if (text.includes("bye") || text.includes("goodbye")) {
    return "Thanks for calling. Have a great day! Goodbye.";
  }

  return `You said: ${msg.text}. How can I help you today?`;
}

async function main(): Promise<void> {
  console.log("Connecting to Patter...");

  await phone.connect({
    onMessage,
    onCallStart: async (data) => {
      console.log(`Incoming call from ${data.caller ?? "unknown"}`);
    },
    onCallEnd: async () => {
      console.log("Call ended");
    },
  });

  console.log("Ready. Waiting for incoming calls... (Ctrl+C to stop)");

  // Keep the process alive
  await new Promise<never>(() => {});
}

process.on("SIGINT", async () => {
  console.log("\nShutting down...");
  await phone.disconnect();
  process.exit(0);
});

main().catch(console.error);
