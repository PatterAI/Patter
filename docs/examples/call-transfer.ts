/**
 * Call transfer — agent escalates to a human when the customer is upset.
 *
 * The transfer_call tool is automatically available on every agent.
 * The agent decides when to use it based on the system prompt.
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
    systemPrompt: `You are a customer service agent for Acme Corp.
If the customer is angry, frustrated, or explicitly asks for a manager or human,
use the transfer_call tool to transfer them to +12345670000 (the manager's line).
Otherwise, help them with their question as best you can.`,
    voice: "alloy",
    firstMessage: "Hello! Thanks for calling Acme. How can I help you today?",
    // transfer_call tool is automatically available — no extra config needed
  });

  console.log("Listening for calls...");
  await phone.serve({ agent, port: 8000 });
}

main().catch(console.error);
