/**
 * @file tool_calling.ts
 * @description In-process tool handlers for a scheduling voice agent.
 *
 * Registers two tools — checkAvailability and bookAppointment — that the voice
 * agent can invoke mid-conversation. Tool results are fed back into the model
 * so the agent can respond naturally with live data.
 *
 * Prerequisites:
 *   - OPENAI_API_KEY, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN in .env
 *
 * Usage:
 *   npx tsx tool_calling.ts
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

// Replace these with real database queries in production.
const BOOKED_SLOTS = new Set(["2026-04-12 14:00", "2026-04-12 15:00"]);

const agent = phone.agent({
  systemPrompt: `You are a scheduling assistant for a dental clinic. Use the
checkAvailability tool to look up open slots and the bookAppointment tool to
confirm bookings. Always verify the patient's preferred date before booking.`,
  voice: "nova",
  firstMessage: "Hello! I can help you schedule an appointment. What date works best for you?",
  tools: [
    Patter.tool({
      name: "checkAvailability",
      description: "Check available appointment slots for a given date and time.",
      parameters: {
        type: "object",
        properties: {
          date: { type: "string", description: "Date in YYYY-MM-DD format" },
          time: { type: "string", description: "Time in HH:MM format (24h)" },
        },
        required: ["date", "time"],
      },
      handler: async ({ date, time }: { date: string; time: string }) => {
        // Connect to your booking system here
        const slot = `${date} ${time}`;
        if (BOOKED_SLOTS.has(slot)) {
          return `Sorry, ${date} at ${time} is already booked. Try another time.`;
        }
        return `The slot on ${date} at ${time} is available.`;
      },
    }),
    Patter.tool({
      name: "bookAppointment",
      description: "Book an appointment at a specific date and time.",
      parameters: {
        type: "object",
        properties: {
          date: { type: "string", description: "Date in YYYY-MM-DD format" },
          time: { type: "string", description: "Time slot in HH:MM format (24h)" },
          name: { type: "string", description: "Patient full name" },
        },
        required: ["date", "time", "name"],
      },
      handler: async ({ date, time, name }: { date: string; time: string; name: string }) => {
        // Write to your database here
        const code = `APT-${date.replace(/-/g, "")}-001`;
        return `Appointment confirmed for ${name} on ${date} at ${time}. Confirmation: ${code}.`;
      },
    }),
  ],
});

async function main(): Promise<void> {
  await phone.serve({ agent, port: 8000 });
}

main();
