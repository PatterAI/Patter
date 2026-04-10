"""Outbound call with recording and answering machine detection."""
import asyncio
from patter import Patter


async def main():
    phone = Patter(
        mode="local",
        twilio_sid="AC...",
        twilio_token="...",
        openai_key="sk-...",
        phone_number="+1...",
        webhook_url="xxx.ngrok-free.dev",
    )

    agent = phone.agent(
        system_prompt="You are calling to confirm an appointment for tomorrow at 3 PM. "
                      "Be brief and polite. If confirmed, thank them and say goodbye.",
        voice="alloy",
        first_message="Hi! I'm calling from Dr. Smith's office to confirm your appointment tomorrow at 3 PM.",
    )

    # recording=True enables call recording for all calls
    await phone.serve(agent=agent, port=8000, recording=True)

    # Allow the server a moment to start before placing the call
    await asyncio.sleep(3)

    # Place an outbound call with answering machine detection
    # If a machine picks up, voicemail_message is played instead of the agent
    await phone.call(
        to="+12345671234",
        machine_detection=True,
        voicemail_message="Hi, this is Dr. Smith's office calling about your appointment tomorrow. "
                          "Please call us back at 555-0123 to confirm. Thank you!",
    )

    print("Call placed. Waiting... (Ctrl+C to stop)")
    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        print("\nShutting down...")


asyncio.run(main())
