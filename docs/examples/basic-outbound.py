"""
Basic outbound call example.

The AI places a call to a destination and holds a conversation.

Usage:
    pip install getpatter
    # Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, OPENAI_API_KEY in the env
    python basic-outbound.py
"""

import asyncio
import os

from getpatter import Patter, Twilio, OpenAIRealtime

PHONE_NUMBER = os.environ.get("PHONE_NUMBER", "+15550001234")
DESTINATION = os.environ.get("DESTINATION", "+14155551234")


async def on_call_start(data: dict) -> None:
    print(f"Call connected (call_id={data.get('call_id')})")


async def on_call_end(data: dict) -> None:
    print(f"Call ended after {data.get('duration_seconds', 0)}s")


async def main() -> None:
    phone = Patter(
        carrier=Twilio(),             # TWILIO_* from env
        phone_number=PHONE_NUMBER,
    )

    agent = phone.agent(
        engine=OpenAIRealtime(),      # OPENAI_API_KEY from env
        system_prompt=(
            "You are calling to confirm an appointment scheduled for tomorrow at 3 PM. "
            "Ask the callee to confirm. If they say yes, thank them and hang up. "
            "If they say no, apologise and offer to reschedule."
        ),
        first_message=(
            "Hi! This is an automated reminder about your appointment tomorrow at 3 PM. "
            "Can you confirm you'll make it?"
        ),
    )

    # Start the server in the background, then place the call
    server_task = asyncio.create_task(
        phone.serve(
            agent,
            tunnel=True,
            on_call_start=on_call_start,
            on_call_end=on_call_end,
        )
    )
    await asyncio.sleep(1)            # wait for the tunnel to come up

    print(f"Calling {DESTINATION}...")
    await phone.call(
        to=DESTINATION,
        agent=agent,
        machine_detection=True,
        voicemail_message="Hi, this is a reminder that your appointment is tomorrow at 3 PM. "
                          "Please call back if you need to reschedule.",
    )

    try:
        await server_task
    except KeyboardInterrupt:
        print("\nShutting down...")


if __name__ == "__main__":
    asyncio.run(main())
