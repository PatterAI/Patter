/**
 * Outbound call with recording and answering machine detection.
 *
 * recording=true enables call recording.
 * machineDetection=true lets the SDK detect voicemail and play a
 * pre-recorded message instead of connecting the live agent.
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
    systemPrompt:
      "You are calling to confirm an appointment for tomorrow at 3 PM. " +
      "Be brief and polite. If confirmed, thank them and say goodbye.",
    firstMessage:
      "Hi! I'm calling from Dr. Smith's office to confirm your appointment tomorrow at 3 PM.",
  });

  // recording: true enables call recording for all calls
  await phone.serve({ agent, port: 8000, recording: true });

  // Allow the server a moment to start before placing the call
  await new Promise((resolve) => setTimeout(resolve, 3000));

  // Place an outbound call with answering machine detection
  // If a machine picks up, voicemailMessage is played instead of the agent
  await phone.call({
    to: "+12345671234",
    machineDetection: true,
    voicemailMessage:
      "Hi, this is Dr. Smith's office calling about your appointment tomorrow. " +
      "Please call us back at 555-0123 to confirm. Thank you!",
  });

  console.log("Call placed. Waiting... (Ctrl+C to stop)");
  await new Promise<never>(() => {});
}

process.on("SIGINT", async () => {
  console.log("\nShutting down...");
  process.exit(0);
});

main().catch(console.error);
