"""
Local mode with custom STT and TTS providers.

Connects using:
- Twilio for telephony
- Deepgram Nova for speech-to-text
- ElevenLabs for text-to-speech

No cloud backend required — runs entirely in your process.

Usage:
    pip install getpatter
    # Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, DEEPGRAM_API_KEY, ELEVENLABS_API_KEY
    python custom-voice.py
"""

import asyncio
import os
from patter import Patter, Twilio, DeepgramSTT, ElevenLabsTTS, IncomingMessage

PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER", "+14155550000")  # E.164 format


async def on_message(msg: IncomingMessage) -> str:
    """Agent logic — returns the text the AI should speak."""
    print(f"[{msg.call_id}] {msg.caller}: {msg.text!r}")

    text = msg.text.lower()

    if "hours" in text or "open" in text:
        return "We are open Monday through Friday from 9 to 5."

    if "price" in text or "cost" in text:
        return "Our pricing starts at $29 per month. Would you like to know more?"

    if "bye" in text or "goodbye" in text or "thanks" in text:
        return "Thank you for calling. Have a wonderful day!"

    return "How can I help you today?"


async def on_call_start(data: dict) -> None:
    print(f"Call started from {data.get('caller')}")


async def on_call_end(data: dict) -> None:
    print(f"Call ended after {data.get('duration_seconds', 0)}s")


async def main() -> None:
    phone = Patter(
        carrier=Twilio(),                               # TWILIO_* from env
        phone_number=PHONE_NUMBER,
    )

    agent = phone.agent(
        stt=DeepgramSTT(),                              # DEEPGRAM_API_KEY from env
        tts=ElevenLabsTTS(voice_id="aria"),             # ELEVENLABS_API_KEY from env
        system_prompt="You are a helpful customer service agent.",
        first_message="Hi! Thanks for calling. How can I help?",
    )

    print(f"Ready on {PHONE_NUMBER}. Waiting for calls... (Ctrl+C to stop)")

    try:
        await phone.serve(
            agent=agent,
            port=8000,
            on_message=on_message,
            on_call_start=on_call_start,
            on_call_end=on_call_end,
        )
    except KeyboardInterrupt:
        print("\nShutting down...")


if __name__ == "__main__":
    asyncio.run(main())
