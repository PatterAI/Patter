"""Dynamic variables in prompts — personalized calls."""
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

    # Placeholders in curly braces are replaced before the call starts.
    # Pass different variables per call to personalise at scale.
    agent = phone.agent(
        system_prompt="""You are a delivery notification assistant for {company_name}.
The customer's name is {customer_name}. Their order #{order_id} is arriving on {delivery_date}.
Confirm the delivery address and ask if they need anything else.""",
        voice="alloy",
        first_message="Hi {customer_name}! This is {company_name} calling about your order #{order_id}.",
        variables={
            "company_name": "FastShip",
            "customer_name": "Mario Rossi",
            "order_id": "FS-2026-789",
            "delivery_date": "tomorrow between 2 and 4 PM",
        },
    )

    print("Listening for calls...")
    await phone.serve(agent=agent, port=8000)


asyncio.run(main())
