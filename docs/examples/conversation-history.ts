/**
 * Conversation history — agent remembers the entire call.
 *
 * on_transcript fires after every turn with the latest text and full history.
 * on_call_end fires when the call finishes with the complete transcript.
 */
import { Patter } from "patter";

interface TranscriptEntry {
  role: "user" | "assistant";
  text: string;
}

interface TranscriptEvent {
  role: "user" | "assistant";
  text: string;
  history: TranscriptEntry[];
}

interface CallEndEvent {
  transcript: TranscriptEntry[];
  duration_seconds?: number;
}

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
    systemPrompt:
      "You are a helpful assistant. " +
      "Reference earlier parts of the conversation when relevant.",
    voice: "alloy",
    firstMessage:
      "Hello! Let's have a conversation. I'll remember everything we discuss.",
  });

  await phone.serve({
    agent,
    port: 8000,

    // Called after every turn with latest text and full history
    onTranscript: async (data: TranscriptEvent) => {
      console.log(`[${data.role}] ${data.text}`);
      console.log(`  History length: ${data.history.length} turns`);
    },

    // Called when the call finishes — full transcript available here
    onCallEnd: async (data: CallEndEvent) => {
      const transcript = data.transcript ?? [];
      console.log("\n=== Call Summary ===");
      for (const entry of transcript) {
        console.log(`  ${entry.role}: ${entry.text}`);
      }
      console.log(`  Total turns: ${transcript.length}`);
    },
  });
}

main().catch(console.error);
