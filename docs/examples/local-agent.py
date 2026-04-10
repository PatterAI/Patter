"""Local mode — AI agent answers calls, no cloud needed."""
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
        system_prompt="You are a friendly customer service agent for Acme Corp.",
        voice="alloy",
        first_message="Hello! Thanks for calling Acme. How can I help?",
    )

    print("Listening for calls...")
    await phone.serve(agent=agent, port=8000)


asyncio.run(main())
