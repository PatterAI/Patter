/**
 * @file full_production.ts
 * @description Complete production-ready Patter setup with all features enabled.
 *
 * This example demonstrates every major SDK capability in one file:
 *   - Pipeline mode with custom LLM (Anthropic Claude)
 *   - Tools (function calling) with local handlers
 *   - Output guardrails for compliance
 *   - Dynamic variables for per-call personalization
 *   - Call recording via Twilio
 *   - Dashboard with token authentication
 *   - Custom pricing overrides for accurate cost tracking
 *   - All lifecycle callbacks: onCallStart, onCallEnd, onMetrics, onMessage
 *
 * Prerequisites:
 *   - All credentials in .env (see below)
 *   - WEBHOOK_URL pointing to this server (e.g. ngrok or load balancer)
 *
 * Usage:
 *   npx ts-node full_production.ts
 */

import { Patter, deepgram, elevenlabs } from "getpatter";
import dotenv from "dotenv";

dotenv.config();

const phone = new Patter({
  mode: "local",
  twilioSid: process.env.TWILIO_ACCOUNT_SID!,
  twilioToken: process.env.TWILIO_AUTH_TOKEN!,
  phoneNumber: process.env.TWILIO_PHONE_NUMBER!,
  webhookUrl: process.env.WEBHOOK_URL!,
});

const agent = phone.agent({
  systemPrompt: `You are a senior account manager for {company_name}.
The caller is {caller_name} (account #{account_id}).
Help them with billing, upgrades, and technical questions.
Never discuss competitor pricing. Never share internal metrics.`,
  provider: "pipeline",
  stt: deepgram({ apiKey: process.env.DEEPGRAM_API_KEY! }),
  tts: elevenlabs({ apiKey: process.env.ELEVENLABS_API_KEY!, voice: "rachel" }),
  voice: "rachel",
  firstMessage: "Hi {caller_name}, thanks for calling {company_name}. How can I help?",
  variables: {
    company_name: "Acme Corp",
    caller_name: "Valued Customer",
    account_id: "000000",
  },
  tools: [
    {
      name: "lookup_account",
      description: "Look up a customer account by phone number or email.",
      parameters: {
        type: "object",
        properties: {
          query: { type: "string", description: "Phone number or email" },
        },
        required: ["query"],
      },
      handler: async (args) => {
        // Replace with your CRM / database lookup
        console.log(`Tool: lookup_account("${args.query}")`);
        return JSON.stringify({ name: "Jane Doe", plan: "Enterprise", balance: "$0.00" });
      },
    },
    {
      name: "create_ticket",
      description: "Create a support ticket in the ticketing system.",
      parameters: {
        type: "object",
        properties: {
          subject: { type: "string" },
          priority: { type: "string", enum: ["low", "medium", "high"] },
        },
        required: ["subject"],
      },
      handler: async (args) => {
        console.log(`Tool: create_ticket("${args.subject}", ${args.priority})`);
        return JSON.stringify({ ticketId: "TK-4821", status: "created" });
      },
    },
  ],
  guardrails: [
    {
      name: "competitor-mention",
      blockedTerms: ["competitor-x", "rival-corp"],
      replacement: "I can only discuss our own products and services.",
    },
    {
      name: "pii-filter",
      check: (text: string) => /\b\d{3}-\d{2}-\d{4}\b/.test(text),
      replacement: "I'm not able to share sensitive personal information over the phone.",
    },
  ],
});

/** Call Anthropic Claude — swap with any LLM endpoint. */
async function callLLM(transcript: string, callId: string): Promise<string> {
  try {
    const response = await fetch("https://api.anthropic.com/v1/messages", {
      method: "POST",
      headers: {
        "x-api-key": process.env.ANTHROPIC_API_KEY!,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
      },
      body: JSON.stringify({
        model: "claude-sonnet-4-20250514",
        max_tokens: 300,
        system: agent.systemPrompt,
        messages: [{ role: "user", content: transcript }],
      }),
    });

    const body = (await response.json()) as Record<string, unknown>;
    const content = body.content as Array<{ text: string }>;
    return content[0].text;
  } catch (error: unknown) {
    const message = error instanceof Error ? error.message : "Unknown LLM error";
    console.error(`LLM call failed for ${callId}: ${message}`);
    return "I'm sorry, I'm experiencing a temporary issue. Please hold.";
  }
}

async function main(): Promise<void> {
  await phone.serve({
    agent,
    port: 8000,
    recording: true,
    dashboard: true,
    dashboardToken: process.env.DASHBOARD_TOKEN!,

    pricing: {
      deepgram: { pricePerMinute: 0.0059 },
      elevenlabs: { pricePerCharacter: 0.00003 },
      twilio: { pricePerMinute: 0.014 },
    },

    onCallStart: async (data) => {
      console.log(`[START] Call ${data.callId} from ${data.caller}`);
      // Return overrides to personalize the agent per-call
      // e.g. look up caller in CRM and inject their name
    },

    onCallEnd: async (data) => {
      console.log(`[END] Call ${data.callId} — ${data.duration}s, ${data.turns} turns`);
      // Save transcript and metrics to your data store
      // await db.insert("call_logs", {
      //   callId: data.callId,
      //   transcript: data.transcript,
      //   duration: data.duration,
      //   cost: data.cost,
      //   endedAt: new Date().toISOString(),
      // });
    },

    onMetrics: async (data) => {
      // Forward per-turn metrics to your monitoring stack
      // await prometheus.gauge("patter_latency_ms", data.latencyMs);
      // await prometheus.counter("patter_turns_total").inc();
      if ((data.latencyMs as number) > 3000) {
        console.warn(`[ALERT] High latency: ${data.latencyMs}ms on call ${data.callId}`);
      }
    },

    onMessage: async (data) => {
      const transcript = data.transcript as string;
      const callId = data.callId as string;
      return callLLM(transcript, callId);
    },
  });

  console.log("Production agent running on port 8000");
  console.log("Dashboard: http://localhost:8000/dashboard");
}

main();
