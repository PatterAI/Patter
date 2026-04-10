"""
Self-hosted mode with custom STT and TTS providers.

Connects to your own Patter backend with:
- Telnyx for telephony
- Deepgram Nova for speech-to-text
- ElevenLabs for text-to-speech

Requires a running Patter backend (see docs/self-hosting.md).

Usage:
    pip install patter
    python custom-voice.py
"""

import asyncio
import os
from patter import Patter, IncomingMessage

# Configuration — use environment variables in production
PATTER_API_KEY = os.environ.get("PATTER_API_KEY", "pt_your_api_key_here")
TELNYX_API_KEY = os.environ.get("TELNYX_API_KEY", "KEY4...")
DEEPGRAM_API_KEY = os.environ.get("DEEPGRAM_API_KEY", "dg_...")
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "el_...")
PHONE_NUMBER = os.environ.get("PHONE_NUMBER", "+14155550000")  # E.164 format

BACKEND_WS = os.environ.get("PATTER_BACKEND_WS", "ws://localhost:8000")
BACKEND_REST = os.environ.get("PATTER_BACKEND_REST", "http://localhost:8000")


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
    print(f"Call started from {data.get('caller')} (mode: {data.get('mode')})")


async def on_call_end(data: dict) -> None:
    print(f"Call ended after {data.get('duration_seconds', 0)}s")


async def main() -> None:
    phone = Patter(
        api_key=PATTER_API_KEY,
        backend_url=BACKEND_WS,
        rest_url=BACKEND_REST,
    )

    print(f"Connecting to {BACKEND_WS}...")
    await phone.connect(
        on_message=on_message,
        on_call_start=on_call_start,
        on_call_end=on_call_end,
        # Self-hosted: pass provider and voice config
        provider="telnyx",
        provider_key=TELNYX_API_KEY,
        number=PHONE_NUMBER,
        country="US",
        stt=Patter.deepgram(api_key=DEEPGRAM_API_KEY, language="en"),
        tts=Patter.elevenlabs(api_key=ELEVENLABS_API_KEY, voice="aria"),
    )

    print(f"Ready on {PHONE_NUMBER}. Waiting for calls... (Ctrl+C to stop)")

    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        print("\nShutting down...")
        await phone.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
