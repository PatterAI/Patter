/**
 * Local mode — AI agent answers calls, no cloud needed.
 */
import { Patter, Twilio, OpenAIRealtime } from "getpatter";

async function main() {
  const phone = new Patter({
    carrier: new Twilio(),                              // TWILIO_* from env
    phoneNumber: "+15550001234",
    webhookUrl: "xxx.ngrok-free.dev",
  });

  const agent = phone.agent({
    engine: new OpenAIRealtime({ voice: "alloy" }),     // OPENAI_API_KEY from env
    systemPrompt: "You are a friendly customer service agent for Acme Corp.",
    firstMessage: "Hello! Thanks for calling Acme. How can I help?",
  });

  console.log("Listening for calls...");
  await phone.serve({ agent, port: 8000 });
}

main().catch(console.error);
