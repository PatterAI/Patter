"""
Call Transfer
=============
Dynamic call transfer and hangup using CallControl. The on_message handler
receives a CallControl object as its second argument, enabling mid-call
actions like transferring to a human agent or ending the conversation.

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

HUMAN_AGENT_NUMBER = "+15550001111"

# ── Initialise ───────────────────────────────────────────────────────
phone = Patter(
    mode="local",
    openai_key=os.getenv("OPENAI_API_KEY"),
    twilio_sid=os.getenv("TWILIO_ACCOUNT_SID"),
    twilio_token=os.getenv("TWILIO_AUTH_TOKEN"),
    phone_number=os.getenv("TWILIO_PHONE_NUMBER"),
    webhook_url=os.getenv("WEBHOOK_URL"),
)

# ── Agent ────────────────────────────────────────────────────────────
agent = phone.agent(
    system_prompt=(
        "You are a friendly first-line support agent. Try to resolve the "
        "caller's issue yourself. If they ask to speak to a human or a "
        "manager, let them know you're transferring them. If they say "
        "goodbye, wish them well and end the call."
    ),
    voice="alloy",
    first_message="Hi there! I'm here to help — what can I do for you?",
)


# ── Message handler with call control ────────────────────────────────
async def on_message(data: dict, call_control) -> str | None:
    """Handle user messages with the ability to transfer or hang up."""
    user_text = data.get("text", "").lower()

    # Check if the call has already ended (transfer or hangup)
    if call_control.ended:
        return None

    # Transfer to a human agent
    if any(phrase in user_text for phrase in ("speak to human", "speak to a person", "transfer me", "talk to a manager")):
        await call_control.transfer(HUMAN_AGENT_NUMBER)
        return "Sure, let me transfer you to a team member right now."

    # End the call gracefully
    if any(phrase in user_text for phrase in ("goodbye", "bye", "that's all", "hang up")):
        await call_control.hangup()
        return "Thanks for calling! Have a great day."

    # Fall back to the LLM for everything else
    return None


# ── Start the server ─────────────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(phone.serve(agent, port=8000, on_message=on_message))
