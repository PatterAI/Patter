"""
Basic inbound call handler.

The AI answers incoming calls and responds to what the caller says.
Replace the API key with your own from https://www.getpatter.com.

Usage:
    pip install getpatter
    python basic-inbound.py
"""

import asyncio
from patter import Patter, IncomingMessage


async def on_message(msg: IncomingMessage) -> str:
    """Called for every utterance the caller makes. Return what the AI should say."""
    print(f"Caller said: {msg.text!r}")

    text = msg.text.lower()

    if "hours" in text or "open" in text:
        return "We are open Monday through Friday, 9 AM to 6 PM Eastern time."

    if "help" in text or "support" in text:
        return "I can help you with billing, technical questions, or account changes. What do you need?"

    if "bye" in text or "goodbye" in text:
        return "Thanks for calling. Have a great day! Goodbye."

    return f"You said: {msg.text}. How can I help you today?"


async def on_call_start(data: dict) -> None:
    print(f"Incoming call from {data.get('caller', 'unknown')}")


async def on_call_end(data: dict) -> None:
    print("Call ended")


async def main() -> None:
    phone = Patter(api_key="pt_your_api_key_here")

    print("Connecting to Patter...")
    await phone.connect(
        on_message=on_message,
        on_call_start=on_call_start,
        on_call_end=on_call_end,
    )
    print("Ready. Waiting for incoming calls... (Ctrl+C to stop)")

    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        print("\nShutting down...")
        await phone.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
