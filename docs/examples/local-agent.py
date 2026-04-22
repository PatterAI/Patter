"""Local mode — AI agent answers calls, no cloud needed."""
import asyncio
from patter import Patter, Twilio, OpenAIRealtime


async def main():
    phone = Patter(
        carrier=Twilio(),                               # TWILIO_* from env
        phone_number="+15550001234",
        webhook_url="xxx.ngrok-free.dev",
    )

    agent = phone.agent(
        engine=OpenAIRealtime(voice="alloy"),           # OPENAI_API_KEY from env
        system_prompt="You are a friendly customer service agent for Acme Corp.",
        first_message="Hello! Thanks for calling Acme. How can I help?",
    )

    print("Listening for calls...")
    await phone.serve(agent=agent, port=8000)


asyncio.run(main())
