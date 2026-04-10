"""
Tool Calling
=============
Voice agent with in-process tool handlers. The agent can check
availability and book appointments by invoking locally defined
Python functions during the conversation.

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

# ── Tool handlers ────────────────────────────────────────────────────
# Replace these with real database queries in production.

async def check_availability(arguments: dict, context: dict) -> str:
    """Check if a given date/time slot is available."""
    date = arguments.get("date", "")
    time = arguments.get("time", "")
    # Connect to your booking system here
    booked = {"2026-04-12 14:00", "2026-04-12 15:00"}
    available = f"{date} {time}" not in booked
    if available:
        return f"The slot on {date} at {time} is available."
    return f"Sorry, {date} at {time} is already booked. Try another time."


async def book_appointment(arguments: dict, context: dict) -> str:
    """Book an appointment for the caller."""
    name = arguments.get("name", "")
    date = arguments.get("date", "")
    time = arguments.get("time", "")
    # Write to your database here
    return f"Appointment confirmed for {name} on {date} at {time}. Confirmation code: APT-{date.replace('-', '')}-001."


# ── Define the voice agent with tools ─────────────────────────────────
agent = phone.agent(
    system_prompt=(
        "You are a scheduling assistant for Bright Smile Dental. "
        "Use the check_availability tool to see if a slot is open, "
        "then use book_appointment to confirm the booking. Always "
        "confirm the details with the caller before booking."
    ),
    voice="nova",
    first_message="Hi! I can help you schedule a dental appointment. What date works for you?",
    tools=[
        Patter.tool(
            name="check_availability",
            description="Check whether a specific date and time slot is available.",
            parameters={
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Date in YYYY-MM-DD format"},
                    "time": {"type": "string", "description": "Time in HH:MM format (24h)"},
                },
                "required": ["date", "time"],
            },
            handler=check_availability,
        ),
        Patter.tool(
            name="book_appointment",
            description="Book an appointment for a customer at the given date and time.",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Customer's full name"},
                    "date": {"type": "string", "description": "Date in YYYY-MM-DD format"},
                    "time": {"type": "string", "description": "Time in HH:MM format (24h)"},
                },
                "required": ["name", "date", "time"],
            },
            handler=book_appointment,
        ),
    ],
)

# ── Start the server ──────────────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(phone.serve(agent, port=8000))
