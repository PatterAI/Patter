"""
Basic Outbound Call
====================
Place an outbound call with answering-machine detection. If a voicemail
is detected the agent leaves a pre-recorded message and hangs up.
Includes on_call_start and on_call_end lifecycle callbacks.

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

# ── Lifecycle callbacks ───────────────────────────────────────────────

async def on_call_start(call):
    """Fires when the call is connected."""
    print(f"[started] Call {call.id} connected to {call.to}")


async def on_call_end(call):
    """Fires when the call ends (either party hangs up)."""
    print(f"[ended]   Call {call.id} — duration {call.duration}s")


# ── Define the voice agent ────────────────────────────────────────────
agent = phone.agent(
    system_prompt=(
        "You are calling to remind the patient about their upcoming "
        "dental appointment on Friday at 2:30 PM with Dr. Patel. "
        "Confirm their attendance, offer to reschedule if needed, "
        "and remind them to bring their insurance card. Be polite "
        "and keep the call under one minute."
    ),
    voice="nova",
    first_message=(
        "Hi, this is a friendly reminder from Bright Smile Dental. "
        "You have an appointment this Friday at 2:30 PM with Dr. Patel. "
        "Will you be able to make it?"
    ),
)

# ── Place the outbound call ───────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(
        phone.call(
            to="+15551234567",  # Replace with the recipient's number
            agent=agent,
            machine_detection=True,
            voicemail_message=(
                "Hi, this is Bright Smile Dental reminding you about your "
                "appointment this Friday at 2:30 PM with Dr. Patel. "
                "Please call us back at 555-987-6543 to confirm. Thank you!"
            ),
            on_call_start=on_call_start,
            on_call_end=on_call_end,
        )
    )
