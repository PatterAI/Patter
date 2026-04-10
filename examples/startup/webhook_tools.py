"""
Webhook Tools
=============
Define tools that POST to external webhook URLs instead of running local
handlers. Perfect for B2B startups that keep business logic in their own
backend and only use Patter for the voice layer.

Requirements:
    pip install patter python-dotenv

Environment variables (.env):
    OPENAI_API_KEY      - OpenAI API key
    TWILIO_ACCOUNT_SID  - Twilio account SID
    TWILIO_AUTH_TOKEN   - Twilio auth token
    TWILIO_PHONE_NUMBER - Your Twilio phone number (E.164 format)
    WEBHOOK_URL         - Public URL where Twilio can reach this server
"""

import asyncio
import os

from dotenv import load_dotenv

load_dotenv()

from patter import Patter

# ── Initialise ───────────────────────────────────────────────────────
phone = Patter(
    mode="local",
    openai_key=os.getenv("OPENAI_API_KEY"),
    twilio_sid=os.getenv("TWILIO_ACCOUNT_SID"),
    twilio_token=os.getenv("TWILIO_AUTH_TOKEN"),
    phone_number=os.getenv("TWILIO_PHONE_NUMBER"),
    webhook_url=os.getenv("WEBHOOK_URL"),
)

# ── Tools backed by external webhooks ────────────────────────────────
lookup_customer = Patter.tool(
    name="lookup_customer",
    description="Look up a customer by email or phone number.",
    parameters={
        "type": "object",
        "properties": {
            "email": {"type": "string", "description": "Customer email address"},
            "phone": {"type": "string", "description": "Customer phone number"},
        },
        "required": [],
    },
    webhook_url="https://api.example.com/customers/lookup",
)

create_ticket = Patter.tool(
    name="create_ticket",
    description="Create a support ticket for the customer.",
    parameters={
        "type": "object",
        "properties": {
            "customer_id": {"type": "string", "description": "Customer ID"},
            "subject": {"type": "string", "description": "Ticket subject"},
            "priority": {
                "type": "string",
                "enum": ["low", "medium", "high"],
                "description": "Ticket priority level",
            },
        },
        "required": ["customer_id", "subject"],
    },
    webhook_url="https://api.example.com/tickets/create",
)

# ── Agent with webhook-backed tools ──────────────────────────────────
agent = phone.agent(
    system_prompt=(
        "You are a customer support agent for Acme Corp. "
        "Use lookup_customer to find the caller's account, then help "
        "resolve their issue. If the issue cannot be resolved on the call, "
        "use create_ticket to open a support ticket. Always confirm the "
        "ticket number with the customer before ending the call."
    ),
    voice="nova",
    first_message="Thanks for calling Acme Corp support! How can I help you today?",
    tools=[lookup_customer, create_ticket],
)

# ── Start the server ─────────────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(phone.serve(agent, port=8000))
