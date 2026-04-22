"""
Basic inbound call handler.

The AI answers incoming calls and responds to what the caller says.
Uses OpenAI Realtime as the default engine — swap for ElevenLabs ConvAI or
pipeline mode (stt=/tts=) if you want full control.

Usage:
    pip install getpatter
    # Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, OPENAI_API_KEY in the env
    python basic-inbound.py
"""

import asyncio
import os

from getpatter import Patter, Twilio, OpenAIRealtime

PHONE_NUMBER = os.environ.get("PHONE_NUMBER", "+15550001234")


async def on_call_start(data: dict) -> None:
    print(f"Incoming call from {data.get('caller', 'unknown')}")


async def on_call_end(data: dict) -> None:
    duration = data.get("duration_seconds", 0)
    print(f"Call ended after {duration}s")


async def on_transcript(event: dict) -> None:
    print(f"[{event['role']}]: {event['text']}")


async def main() -> None:
    phone = Patter(
        carrier=Twilio(),             # reads TWILIO_ACCOUNT_SID + TWILIO_AUTH_TOKEN
        phone_number=PHONE_NUMBER,
    )

    agent = phone.agent(
        engine=OpenAIRealtime(),      # reads OPENAI_API_KEY
        system_prompt=(
            "You are the receptionist for Acme Corp. Help callers with hours, "
            "support questions, and simple account changes. Keep replies short."
        ),
        first_message="Hi! Thanks for calling Acme Corp. How can I help?",
    )

    print(f"Ready on {PHONE_NUMBER}. Waiting for calls... (Ctrl+C to stop)")

    try:
        await phone.serve(
            agent,
            tunnel=True,              # start a Cloudflare Quick Tunnel for dev
            on_call_start=on_call_start,
            on_call_end=on_call_end,
            on_transcript=on_transcript,
        )
    except KeyboardInterrupt:
        print("\nShutting down...")


if __name__ == "__main__":
    asyncio.run(main())
