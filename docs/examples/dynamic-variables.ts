/**
 * Dynamic variables in prompts — personalized calls.
 *
 * Placeholders in curly braces are replaced before the call starts.
 * Pass different variables per call to personalise at scale.
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
    systemPrompt: `You are a delivery notification assistant for {company_name}.
The customer's name is {customer_name}. Their order #{order_id} is arriving on {delivery_date}.
Confirm the delivery address and ask if they need anything else.`,
    voice: "alloy",
    firstMessage:
      "Hi {customer_name}! This is {company_name} calling about your order #{order_id}.",
    variables: {
      company_name: "FastShip",
      customer_name: "Mario Rossi",
      order_id: "FS-2026-789",
      delivery_date: "tomorrow between 2 and 4 PM",
    },
  });

  console.log("Listening for calls...");
  await phone.serve({ agent, port: 8000 });
}

main().catch(console.error);
