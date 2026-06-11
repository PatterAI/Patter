/**
 * Preemptive generation — pay LLM+TTS latency during the caller's own pause.
 *
 * Pipeline mode with the built-in LLM loop only. With preemptiveGeneration:
 * true the SDK starts the LLM (and sentence-chunked TTS synthesis) EARLY on a
 * confident interim transcript — one that ends with sentence-final
 * punctuation, or that has been unchanged for preemptiveMinStableMs — holding
 * the audio in memory. If the final transcript matches the speculated interim,
 * the buffered audio is released to the carrier immediately; if it differs,
 * the speculation is discarded silently and the turn dispatches normally.
 */
import { Patter, Twilio, DeepgramSTT, OpenAILLM, ElevenLabsTTS } from "getpatter";

async function main() {
  const phone = new Patter({
    carrier: new Twilio(),                              // TWILIO_* from env
    phoneNumber: "+15550001234",
    webhookUrl: "xxx.ngrok-free.dev",
  });

  const agent = phone.agent({
    stt: new DeepgramSTT(),                             // DEEPGRAM_API_KEY from env
    llm: new OpenAILLM(),                               // OPENAI_API_KEY from env
    tts: new ElevenLabsTTS({ voiceId: "aria" }),        // ELEVENLABS_API_KEY from env
    systemPrompt:
      "You are a quick, friendly assistant for Acme Corp. Keep replies short.",
    firstMessage: "Hi! Thanks for calling Acme. How can I help?",
    preemptiveGeneration: true,  // default false — opt in to speculation
    preemptiveMinStableMs: 300,  // default 300 — interim without sentence-final
                                 // punctuation must hold steady this long
  });

  console.log("Listening for calls...");
  await phone.serve({ agent, port: 8000 });
}

main().catch(console.error);
