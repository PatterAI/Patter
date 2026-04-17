"""
Local mode with custom STT and TTS providers.

Connects using local mode with:
- Twilio for telephony
- Deepgram Nova for speech-to-text
- ElevenLabs for text-to-speech

No cloud backend required — runs entirely in your process.

Usage:
    pip install getpatter
    python custom-voice.py
"""

import asyncio
import os
from patter import Patter, IncomingMessage

# Configuration — use environment variables in production
TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID", "AC...")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "your_auth_token")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "sk-...")
DEEPGRAM_API_KEY = os.environ.get("DEEPGRAM_API_KEY", "dg_...")
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "el_...")
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
        twilio_sid=TWILIO_SID,
        twilio_token=TWILIO_TOKEN,
        openai_key=OPENAI_KEY,
        phone_number=PHONE_NUMBER,
    )

    agent = phone.agent(
        system_prompt="You are a helpful customer service agent.",
        voice="alloy",
        first_message="Hi! Thanks for calling. How can I help?",
    )

    print(f"Ready on {PHONE_NUMBER}. Waiting for calls... (Ctrl+C to stop)")

    try:
        await phone.serve(
            agent=agent,
            port=8000,
            on_call_start=on_call_start,
            on_call_end=on_call_end,
            # Use custom STT and TTS providers
            stt=Patter.deepgram(api_key=DEEPGRAM_API_KEY, language="en"),
            tts=Patter.elevenlabs(api_key=ELEVENLABS_API_KEY, voice="aria"),
        )
    except KeyboardInterrupt:
        print("\nShutting down...")


if __name__ == "__main__":
    asyncio.run(main())
