"""
Test Mode
==========
Run the voice agent locally without telephony for rapid iteration.
Messages are exchanged via the terminal. Useful for testing prompts,
tools, and conversation flow before connecting a real phone number.

Interactive commands (type in the terminal):
    /quit      - End the test session
    /transfer  - Simulate a call transfer
    /hangup    - Simulate the caller hanging up
    /history   - Print the full conversation history

Requirements:
    pip install patter python-dotenv

Environment variables (.env):
    OPENAI_API_KEY - OpenAI API key
"""

import asyncio
import os

from dotenv import load_dotenv

load_dotenv()

from patter import Patter

# ── Initialise in local mode (no Twilio credentials needed) ───────────
phone = Patter(
    mode="local",
    openai_key=os.getenv("OPENAI_API_KEY"),
)

# ── Lifecycle callbacks ───────────────────────────────────────────────

async def on_call_start(data: dict) -> None:
    """Fires when the test call begins."""
    print(f"  Call started: {data.get('call_id')}")


async def on_call_end(data: dict) -> None:
    """Fires when the test call ends."""
    transcript = data.get("transcript", [])
    print(f"  Call ended — {len(transcript)} messages exchanged")


# ── Define the voice agent ────────────────────────────────────────────
agent = phone.agent(
    system_prompt=(
        "You are a helpful customer support agent for Acme Corp. "
        "Answer questions about orders, returns, and shipping. "
        "If you cannot help, offer to transfer the caller to a human agent."
    ),
    voice="nova",
    first_message="Hello! Thanks for contacting Acme Corp support. How can I help you?",
)

# ── Run in test mode (terminal I/O, no telephony) ────────────────────
if __name__ == "__main__":
    asyncio.run(
        phone.test(
            agent,
            on_call_start=on_call_start,
            on_call_end=on_call_end,
        )
    )
