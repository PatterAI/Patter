/**
 * E2E test server — starts an EmbeddedServer on port 8765 with mock telephony.
 *
 * Playwright's webServer config runs this script before the test suite.
 * No real telephony providers are contacted; the server exposes the dashboard
 * and API routes which E2E specs exercise via HTTP + browser navigation.
 */

import { EmbeddedServer } from "../../src/server";

const server = new EmbeddedServer(
  {
    phoneNumber: "+15551234567",
    twilioSid: "ACtest123456789",
    twilioToken: "",          // empty → signature validation skipped
    webhookUrl: "example.com",
  },
  {
    systemPrompt: "You are a test agent for E2E testing.",
    provider: "openai_realtime",
  },
  undefined, // onCallStart
  undefined, // onCallEnd
  undefined, // onTranscript
  undefined, // onMessage
  false,     // recording
  "",        // voicemailMessage
  undefined, // onMetrics
  undefined, // pricingOverrides
  true,      // dashboard enabled
  "",        // dashboardToken (no auth for tests)
);

server.start(8765).then(() => {
  console.log("E2E test server listening on port 8765");
}).catch((err) => {
  console.error("Failed to start E2E test server:", err);
  process.exit(1);
});
