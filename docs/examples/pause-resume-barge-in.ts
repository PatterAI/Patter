/**
 * Pause-and-resume barge-in — survive coughs and line noise (pipeline mode).
 *
 * With bargeInMode: 'pause_resume', a VAD speech_start during the agent's turn
 * PAUSES playback instead of cancelling it. If a committed final transcript
 * confirms a real interruption within bargeInConfirmMs, the turn is cancelled
 * as usual; if the window expires with no transcript (a cough, background
 * noise), the agent resumes from the first sentence the caller had not fully
 * heard — without re-billing TTS.
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
      "You are a patient customer-support agent for Acme Corp. " +
      "Answer thoroughly — callers can interrupt you at any time.",
    firstMessage: "Hi! Thanks for calling Acme. How can I help you today?",
    bargeInMode: "pause_resume", // default 'cancel': kill the turn on speech_start
    bargeInConfirmMs: 1500,      // default 1500 — resume TTS if no final
                                 // transcript confirms within this window
  });

  console.log("Listening for calls...");
  await phone.serve({ agent, port: 8000 });
}

main().catch(console.error);
