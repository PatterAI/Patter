/**
 * Pipeline mode — use YOUR agent to handle conversations.
 *
 * Bring your own LLM (Claude, GPT, LangChain, or custom logic).
 * Patter handles STT, TTS, and telephony; your function handles the brain.
 */
import { Patter } from "getpatter";

interface TurnData {
  text: string;
  history: Array<{ role: "user" | "assistant"; text: string }>;
}

// Your agent — can be Claude, GPT, LangChain, custom logic, anything.
// Receives the latest transcript and full call history.
// Returns the text the AI should speak.
async function myAgent(data: TurnData): Promise<string> {
  const { text, history } = data;

  // Example: Claude via the Anthropic SDK
  // import Anthropic from "@anthropic-ai/sdk";
  // const client = new Anthropic();
  // const response = await client.messages.create({
  //   model: "claude-sonnet-4-20250514",
  //   system: "You are a helpful phone assistant.",
  //   messages: history.map((m) => ({ role: m.role, content: m.text })),
  //   max_tokens: 256,
  // });
  // return (response.content[0] as { text: string }).text;

  // Example: OpenAI
  // import OpenAI from "openai";
  // const client = new OpenAI();
  // const messages = [
  //   { role: "system" as const, content: "You are a helpful phone assistant." },
  //   ...history.map((m) => ({ role: m.role as "user" | "assistant", content: m.text })),
  // ];
  // const response = await client.chat.completions.create({ model: "gpt-4o", messages });
  // return response.choices[0].message.content ?? "";

  // Simple keyword demo
  console.log(`[history=${history.length} turns] user: "${text}"`);
  if (text.toLowerCase().includes("hello")) {
    return "Hi there! I'm running with custom agent logic. How can I help?";
  }
  return `You said: ${text}. I processed this with my own logic, not OpenAI!`;
}

async function main() {
  // openaiKey is not required in pipeline mode — you provide the LLM yourself
  const phone = new Patter({
    mode: "local",
    twilioSid: "AC...",
    twilioToken: "...",
    phoneNumber: "+1...",
    webhookUrl: "xxx.ngrok-free.dev",
  });

  const agent = phone.agent({
    provider: "pipeline",
    stt: Patter.deepgram({ apiKey: "dg_..." }),
    tts: Patter.elevenlabs({ apiKey: "el_...", voice: "aria" }),
    language: "en",
  });

  console.log("Listening for calls...");
  await phone.serve({
    agent,
    port: 8000,
    onMessage: myAgent, // YOUR function handles every turn
  });
}

main().catch(console.error);
