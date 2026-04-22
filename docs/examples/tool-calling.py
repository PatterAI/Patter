"""Agent with tool calling — checks inventory during the call."""
import asyncio
from getpatter import Patter, Twilio, OpenAIRealtime, Tool


async def main():
    phone = Patter(
        carrier=Twilio(),                               # TWILIO_* from env
        phone_number="+15550001234",
        webhook_url="xxx.ngrok-free.dev",
    )

    agent = phone.agent(
        engine=OpenAIRealtime(),                        # OPENAI_API_KEY from env
        system_prompt="You help customers check product availability.",
        tools=[
            Tool(
                name="check_stock",
                description="Check if a product is in stock",
                parameters={"type": "object", "properties": {"product": {"type": "string"}}},
                webhook_url="https://api.example.com/stock",
            ),
        ],
    )

    print("Listening for calls...")
    await phone.serve(agent=agent, port=8000)


asyncio.run(main())
