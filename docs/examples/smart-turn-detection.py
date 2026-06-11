"""Semantic end-of-utterance detection with smart-turn v3 (pipeline mode).

A VAD only knows WHETHER the caller is making sound; smart-turn looks at the
prosody of the last few seconds of speech and predicts whether the caller has
FINISHED their turn or is merely pausing mid-sentence ("My phone number is…").
The pipeline defers the turn until the model agrees the caller is done,
holding for at most max_semantic_hold_ms so a turn can never hang.

Setup:
    pip install "getpatter[turn-detector]"
    # Download a smart-turn-v3 ONNX file (~30 MB, NOT bundled with the SDK)
    # from https://huggingface.co/pipecat-ai/smart-turn-v3 then either:
    export PATTER_SMART_TURN_MODEL=/path/to/smart-turn-v3.0.onnx
    # ...or pass model_path= to SmartTurnDetector.load() below.
"""
import asyncio

from getpatter import (
    Patter,
    Twilio,
    DeepgramSTT,
    OpenAILLM,
    ElevenLabsTTS,
    SmartTurnDetector,
)


async def main():
    phone = Patter(
        carrier=Twilio(),                               # TWILIO_* from env
        phone_number="+15550001234",
        webhook_url="xxx.ngrok-free.dev",
    )

    # Reads PATTER_SMART_TURN_MODEL; or pass model_path="/path/to/model.onnx".
    # threshold=0.5 is the default end-of-turn probability cutoff.
    detector = SmartTurnDetector.load()

    agent = phone.agent(
        stt=DeepgramSTT(),                              # DEEPGRAM_API_KEY from env
        llm=OpenAILLM(),                                # OPENAI_API_KEY from env
        tts=ElevenLabsTTS(voice_id="aria"),             # ELEVENLABS_API_KEY from env
        system_prompt="You are a receptionist for Acme Corp. Collect the "
                      "caller's name and phone number, then confirm them back.",
        first_message="Hi! Thanks for calling Acme. Who am I speaking with?",
        turn_detector=detector,
        max_semantic_hold_ms=1200,  # default 1200 — max wait while the model
                                    # keeps predicting "incomplete"
    )

    print("Listening for calls...")
    await phone.serve(agent=agent, port=8000)


asyncio.run(main())
