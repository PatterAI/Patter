/**
 * @file webhook_tools.ts
 * @description Webhook-based tool integration for voice agents.
 *
 * Instead of inline handler functions, each tool delegates execution to
 * an external webhook URL. This lets you keep business logic in your own
 * backend (CRM lookup, ticketing, etc.) while the agent handles the
 * conversation flow.
 *
 * Prerequisites:
 *   - OPENAI_API_KEY, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN in .env
 *   - Webhook endpoints deployed and reachable
 *   - A Twilio phone number pointed at your webhook URL
 *
 * Usage:
 *   npx ts-node webhook_tools.ts
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
  systemPrompt: `You are a customer support agent for TechCo. Look up the caller's
account using their phone number or email, then help resolve their issue.
If the issue cannot be resolved immediately, create a support ticket and
give the caller the ticket number.`,
  firstMessage: "Thank you for calling TechCo support. Can I have your name or email to pull up your account?",
  tools: [
    {
      name: "lookupCustomer",
      description: "Look up a customer by email or phone number and return their account details.",
      parameters: {
        type: "object",
        properties: {
          email: { type: "string", description: "Customer email address" },
          phone: { type: "string", description: "Customer phone number" },
        },
      },
      webhookUrl: "https://api.example.com/webhooks/patter/lookup-customer",
    },
    {
      name: "createTicket",
      description: "Create a support ticket for an unresolved issue and return the ticket ID.",
      parameters: {
        type: "object",
        properties: {
          customerId: { type: "string", description: "The customer's account ID" },
          subject: { type: "string", description: "Brief summary of the issue" },
          priority: { type: "string", enum: ["low", "medium", "high"], description: "Ticket priority" },
        },
        required: ["customerId", "subject"],
      },
      webhookUrl: "https://api.example.com/webhooks/patter/create-ticket",
    },
  ],
});

async function main(): Promise<void> {
  await phone.serve({ agent, port: 8000 });
  console.log("Webhook-tools agent listening on port 8000");
}

main();
