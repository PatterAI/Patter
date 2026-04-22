"""Call transfer — agent escalates to a human when the customer is upset."""
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
        system_prompt="""You are a customer service agent for Acme Corp.
If the customer is angry, frustrated, or explicitly asks for a manager or human,
use the transfer_call tool to transfer them to +12345670000 (the manager's line).
Otherwise, help them with their question as best you can.""",
        first_message="Hello! Thanks for calling Acme. How can I help you today?",
        # transfer_call tool is automatically available — no extra config needed
    )

    print("Listening for calls...")
    await phone.serve(agent=agent, port=8000)


asyncio.run(main())
