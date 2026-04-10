"""
Output Guardrails
==================
Demonstrate how to attach guardrails that filter or replace agent
output in real time. Two guardrail types are shown:

1. **Blocked terms** -- a simple deny-list that scrubs competitor names.
2. **Custom check** -- a callable that inspects each output chunk and
   substitutes a safe replacement when the check fails.

Requirements:
    pip install patter python-dotenv

Environment variables (.env):
    OPENAI_API_KEY      - OpenAI API key with Realtime access
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

# ── Initialise the Patter client ──────────────────────────────────────
phone = Patter(
    mode="local",
    openai_key=os.getenv("OPENAI_API_KEY"),
    twilio_sid=os.getenv("TWILIO_ACCOUNT_SID"),
    twilio_token=os.getenv("TWILIO_AUTH_TOKEN"),
    phone_number=os.getenv("TWILIO_PHONE_NUMBER"),
    webhook_url=os.getenv("WEBHOOK_URL"),
)

# ── Guardrail 1: block competitor mentions ────────────────────────────
phone.guardrail(
    name="no-competitors",
    blocked_terms=["CompetitorA", "CompetitorB"],
)

# ── Guardrail 2: prevent specific pricing disclosure ─────────────────
phone.guardrail(
    name="no-pricing",
    check=lambda text: "$" not in text,
    replacement="I can't discuss specific pricing on this call.",
)

# ── Define the voice agent with both guardrails applied ───────────────
agent = phone.agent(
    system_prompt=(
        "You are a sales representative for Acme Widgets. Answer "
        "questions about our products, features, and general pricing "
        "tiers (e.g. 'Starter', 'Pro', 'Enterprise'). Never mention "
        "competitor products by name and never quote exact dollar amounts."
    ),
    voice="nova",
    first_message="Hi there! I'm happy to tell you about Acme Widgets. What would you like to know?",
    guardrails=["no-competitors", "no-pricing"],
)

# ── Start the server ──────────────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(phone.serve(agent, port=8000))
