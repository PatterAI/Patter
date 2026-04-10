/**
 * Local mode — AI agent answers calls, no cloud needed.
 */
import { Patter } from "patter";

async function main() {
  const phone = new Patter({
    mode: "local",
    twilioSid: "AC...",
    twilioToken: "...",
    openaiKey: "sk-...",
    phoneNumber: "+1...",
    webhookUrl: "xxx.ngrok-free.dev",
  });

  const agent = phone.agent({
    systemPrompt: "You are a friendly customer service agent for Acme Corp.",
    voice: "alloy",
    firstMessage: "Hello! Thanks for calling Acme. How can I help?",
  });

  console.log("Listening for calls...");
  await phone.serve({ agent, port: 8000 });
}

main().catch(console.error);
