"""Agent with tool calling — checks inventory during the call."""
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
        system_prompt="You help customers check product availability.",
        tools=[{
            "name": "check_stock",
            "description": "Check if a product is in stock",
            "parameters": {"type": "object", "properties": {"product": {"type": "string"}}},
            "webhook_url": "https://api.example.com/stock",
        }],
    )

    print("Listening for calls...")
    await phone.serve(agent=agent, port=8000)


asyncio.run(main())
