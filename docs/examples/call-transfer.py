"""Call transfer — agent escalates to a human when the customer is upset."""
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
        system_prompt="""You are a customer service agent for Acme Corp.
If the customer is angry, frustrated, or explicitly asks for a manager or human,
use the transfer_call tool to transfer them to +12345670000 (the manager's line).
Otherwise, help them with their question as best you can.""",
        voice="alloy",
        first_message="Hello! Thanks for calling Acme. How can I help you today?",
        # transfer_call tool is automatically available — no extra config needed
    )

    print("Listening for calls...")
    await phone.serve(agent=agent, port=8000)


asyncio.run(main())
