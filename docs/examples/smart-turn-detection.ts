/**
 * Semantic end-of-utterance detection with smart-turn v3 (pipeline mode).
 *
 * A VAD only knows WHETHER the caller is making sound; smart-turn looks at
 * the prosody of the last few seconds of speech and predicts whether the
 * caller has FINISHED their turn or is merely pausing mid-sentence ("My phone
 * number is…"). The pipeline defers the turn until the model agrees the
 * caller is done, holding for at most maxSemanticHoldMs so a turn never hangs.
 *
 * Setup:
 *   npm install getpatter          # onnxruntime-node ships as an optional dep
 *   # Download a smart-turn-v3 ONNX file (~30 MB, NOT bundled with the SDK)
 *   # from https://huggingface.co/pipecat-ai/smart-turn-v3 then either:
 *   export PATTER_SMART_TURN_MODEL=/path/to/smart-turn-v3.0.onnx
 *   # ...or pass modelPath to SmartTurnDetector.load() below.
 */
import {
  Patter,
  Twilio,
  DeepgramSTT,
  OpenAILLM,
  ElevenLabsTTS,
  SmartTurnDetector,
} from "getpatter";

async function main() {
  const phone = new Patter({
    carrier: new Twilio(),                              // TWILIO_* from env
    phoneNumber: "+15550001234",
    webhookUrl: "xxx.ngrok-free.dev",
  });

  // Reads PATTER_SMART_TURN_MODEL; or pass { modelPath: "/path/to/model.onnx" }.
  // threshold: 0.5 is the default end-of-turn probability cutoff.
  const detector = await SmartTurnDetector.load();

  const agent = phone.agent({
    stt: new DeepgramSTT(),                             // DEEPGRAM_API_KEY from env
    llm: new OpenAILLM(),                               // OPENAI_API_KEY from env
    tts: new ElevenLabsTTS({ voiceId: "aria" }),        // ELEVENLABS_API_KEY from env
    systemPrompt:
      "You are a receptionist for Acme Corp. Collect the caller's name and " +
      "phone number, then confirm them back.",
    firstMessage: "Hi! Thanks for calling Acme. Who am I speaking with?",
    turnDetector: detector,
    maxSemanticHoldMs: 1200, // default 1200 — max wait while the model
                             // keeps predicting "incomplete"
  });

  console.log("Listening for calls...");
  await phone.serve({ agent, port: 8000 });
}

main().catch(console.error);
