"""
Basic outbound call example.

The AI places a call to a phone number and holds a conversation.
Replace the API key and destination number before running.

Usage:
    pip install getpatter
    python basic-outbound.py
"""

import asyncio
from getpatter import Patter, IncomingMessage

DESTINATION = "+14155551234"   # Replace with a real number
API_KEY = "pt_your_api_key_here"


async def on_message(msg: IncomingMessage) -> str:
    """Handle the callee's responses."""
    print(f"Callee said: {msg.text!r}")

    text = msg.text.lower()

    if "yes" in text or "confirm" in text or "sure" in text:
        return "Perfect. Your appointment is confirmed. We will see you then. Goodbye!"

    if "no" in text or "cancel" in text:
        return "No problem. I have cancelled your appointment. Have a good day. Goodbye!"

    if "when" in text or "time" in text:
        return "Your appointment is scheduled for tomorrow at 3 PM. Can you confirm you will make it?"

    return "I did not catch that. Could you say yes to confirm or no to cancel your appointment?"


async def on_call_start(data: dict) -> None:
    print(f"Call connected (call ID: {data.get('call_id')})")


async def on_call_end(data: dict) -> None:
    duration = data.get("duration_seconds", 0)
    print(f"Call ended after {duration} seconds")


async def main() -> None:
    phone = Patter(api_key=API_KEY)

    print("Connecting to Patter...")
    await phone.connect(
        on_message=on_message,
        on_call_start=on_call_start,
        on_call_end=on_call_end,
    )

    print(f"Calling {DESTINATION}...")
    await phone.call(
        to=DESTINATION,
        first_message="Hi! This is an automated reminder about your appointment tomorrow at 3 PM. "
                      "Can you confirm you will make it?",
    )

    print("Call placed. Waiting for it to complete... (Ctrl+C to stop)")

    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        print("\nShutting down...")
        await phone.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
