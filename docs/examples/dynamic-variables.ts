/**
 * Dynamic variables in prompts — personalized calls.
 *
 * Placeholders in curly braces are replaced before the call starts.
 * Pass different variables per call to personalise at scale.
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
    systemPrompt: `You are a delivery notification assistant for {company_name}.
The customer's name is {customer_name}. Their order #{order_id} is arriving on {delivery_date}.
Confirm the delivery address and ask if they need anything else.`,
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
