"""
Multi-Agent Setup (Multiple Numbers)
=====================================
Run multiple voice agents on separate phone numbers. Each agent has its
own system prompt, voice, and tools — ideal for routing sales and
support to different numbers within the same process.

Requirements:
    pip install patter python-dotenv

Environment variables (.env):
    OPENAI_API_KEY        - OpenAI API key with Realtime access
    TWILIO_ACCOUNT_SID    - Twilio account SID
    TWILIO_AUTH_TOKEN     - Twilio auth token
    SALES_PHONE_NUMBER    - Phone number for the sales agent (E.164)
    SUPPORT_PHONE_NUMBER  - Phone number for the support agent (E.164)
    WEBHOOK_URL_SALES     - Public URL for the sales server
    WEBHOOK_URL_SUPPORT   - Public URL for the support server
"""

import asyncio
import os

from dotenv import load_dotenv

load_dotenv()

from patter import Patter

# ── Sales agent ──────────────────────────────────────────────────────

sales_phone = Patter(
    mode="local",
    openai_key=os.getenv("OPENAI_API_KEY"),
    twilio_sid=os.getenv("TWILIO_ACCOUNT_SID"),
    twilio_token=os.getenv("TWILIO_AUTH_TOKEN"),
    phone_number=os.getenv("SALES_PHONE_NUMBER"),
    webhook_url=os.getenv("WEBHOOK_URL_SALES"),
)

sales_agent = sales_phone.agent(
    system_prompt=(
        "You are a persuasive but honest sales agent for Acme SaaS. "
        "Help callers understand pricing plans, schedule demos, and "
        "close deals. Always ask for their email to send a follow-up."
    ),
    voice="echo",
    first_message="Hey there! Thanks for calling Acme Sales. What can I help you with?",
    tools=[
        Patter.tool(
            name="schedule_demo",
            description="Schedule a product demo for the caller",
            parameters={
                "type": "object",
                "properties": {
                    "email": {"type": "string", "description": "Caller's email"},
                    "preferred_date": {"type": "string", "description": "Preferred date"},
                },
                "required": ["email"],
            },
            webhook_url="https://api.yourcompany.com/demos/schedule",
        ),
    ],
)

# ── Support agent ────────────────────────────────────────────────────

support_phone = Patter(
    mode="local",
    openai_key=os.getenv("OPENAI_API_KEY"),
    twilio_sid=os.getenv("TWILIO_ACCOUNT_SID"),
    twilio_token=os.getenv("TWILIO_AUTH_TOKEN"),
    phone_number=os.getenv("SUPPORT_PHONE_NUMBER"),
    webhook_url=os.getenv("WEBHOOK_URL_SUPPORT"),
)

support_agent = support_phone.agent(
    system_prompt=(
        "You are a patient and knowledgeable support agent for Acme SaaS. "
        "Help callers troubleshoot issues, check account status, and "
        "escalate to a human when needed. Be empathetic and thorough."
    ),
    voice="nova",
    first_message="Hi, you've reached Acme Support. What issue can I help you with?",
    tools=[
        Patter.tool(
            name="lookup_account",
            description="Look up a customer account by email or phone number",
            parameters={
                "type": "object",
                "properties": {
                    "email": {"type": "string"},
                    "phone": {"type": "string"},
                },
            },
            webhook_url="https://api.yourcompany.com/accounts/lookup",
        ),
    ],
)


# ── Run both servers concurrently ────────────────────────────────────
async def main() -> None:
    print("Starting sales agent on port 8000...")
    print("Starting support agent on port 8001...")
    await asyncio.gather(
        sales_phone.serve(sales_agent, port=8000),
        support_phone.serve(support_agent, port=8001),
    )


if __name__ == "__main__":
    asyncio.run(main())
