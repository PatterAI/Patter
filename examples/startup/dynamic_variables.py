"""
Dynamic Variables
=================
Variable substitution in system prompts for personalised agent behaviour.
Define placeholders like {customer_name} in the prompt and supply default
values via the agent config. Override them dynamically at call start based
on the caller's identity.

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

# ── Agent with placeholder variables in the system prompt ────────────
agent = phone.agent(
    system_prompt=(
        "You are a scheduling assistant for Acme Health. "
        "You are speaking with {customer_name} (account {account_number}). "
        "Their next appointment is on {appointment_date}. "
        "Help them reschedule, confirm, or cancel their appointment. "
        "Always address them by name and reference their appointment date."
    ),
    voice="nova",
    first_message="Hi {customer_name}! I see your appointment on {appointment_date}. How can I help?",
    variables={
        "customer_name": "Jane Doe",
        "account_number": "AC-12345",
        "appointment_date": "March 20th",
    },
)

# ── Customer database (keyed by caller phone number) ────────────────
CUSTOMER_DB: dict[str, dict[str, str]] = {
    "+14155551234": {
        "customer_name": "Alice Chen",
        "account_number": "AC-78901",
        "appointment_date": "April 3rd",
    },
    "+14155555678": {
        "customer_name": "Bob Martinez",
        "account_number": "AC-45678",
        "appointment_date": "April 10th",
    },
}


# ── Override variables at call start based on caller ID ──────────────
async def on_call_start(data: dict) -> dict | None:
    """Return dynamic variable overrides based on who is calling."""
    caller = data.get("caller", "")
    customer = CUSTOMER_DB.get(caller)
    if customer:
        print(f"[call_start] Known caller {caller} → {customer['customer_name']}")
        return {"variables": customer}
    print(f"[call_start] Unknown caller {caller} — using default variables")
    return None


# ── Start the server ─────────────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(phone.serve(agent, port=8000, on_call_start=on_call_start))
