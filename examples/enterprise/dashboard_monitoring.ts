/**
 * @file dashboard_monitoring.ts
 * @description Built-in dashboard and analytics for monitoring call quality.
 *
 * Patter ships with a dashboard UI and REST API for real-time observability.
 * Enable it with a single flag and protect it with a bearer token.
 *
 * Dashboard endpoints (served automatically):
 *   GET  /dashboard          — Web UI with live call feed and charts
 *   GET  /api/calls          — JSON list of all calls with metrics
 *   GET  /api/calls/:id      — Single call detail with transcript
 *   GET  /api/export/csv     — Export calls as CSV
 *   GET  /api/export/json    — Export calls as JSON
 *   GET  /api/sse            — Server-Sent Events stream for live updates
 *
 * All /api/* routes require:
 *   Authorization: Bearer <dashboardToken>
 *
 * Prerequisites:
 *   - OPENAI_API_KEY, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN in .env
 *   - TWILIO_PHONE_NUMBER, WEBHOOK_URL in .env
 *   - DASHBOARD_TOKEN in .env (any secret string)
 *
 * Usage:
 *   npx ts-node dashboard_monitoring.ts
 */

import { Patter } from "patter";
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
  systemPrompt: `You are a customer success agent for Acme Corp.
Help customers with billing, account, and product questions.`,
  voice: "nova",
  firstMessage: "Welcome to Acme Corp support. How can I help?",
});

async function main(): Promise<void> {
  await phone.serve({
    agent,
    port: 8000,
    dashboard: true,
    dashboardToken: process.env.DASHBOARD_TOKEN ?? "change-me-in-production",

    onCallEnd: async (data) => {
      // Save metrics to your own data warehouse alongside the dashboard
      const metrics = {
        callId: data.callId,
        duration: data.duration,
        turns: data.turns,
        avgLatency: data.avgLatency,
        cost: data.cost,
        timestamp: new Date().toISOString(),
      };
      console.log("Call metrics:", JSON.stringify(metrics));
      // await dataWarehouse.insert("call_metrics", metrics);
    },

    onMetrics: async (data) => {
      // Per-turn metrics — useful for latency alerting
      if ((data.latencyMs as number) > 2000) {
        console.warn(`High latency detected: ${data.latencyMs}ms on turn ${data.turn}`);
        // await alerting.send("patter-latency", data);
      }
    },
  });

  console.log("Dashboard available at http://localhost:8000/dashboard");
  console.log("API available at http://localhost:8000/api/calls");
}

main();
