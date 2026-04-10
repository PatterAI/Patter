"""
Pipeline Custom Voice
=====================
Pipeline mode with Deepgram STT + ElevenLabs TTS for startups that want
granular control over their speech providers. This is ideal for optimising
cost and latency by choosing best-in-class providers independently.

Requirements:
    pip install patter python-dotenv

Environment variables (.env):
    OPENAI_API_KEY      - OpenAI API key (used for the LLM turn)
    DEEPGRAM_API_KEY    - Deepgram API key for speech-to-text
    ELEVENLABS_API_KEY  - ElevenLabs API key for text-to-speech
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

# ── Initialise the Patter client with all provider keys ──────────────
phone = Patter(
    mode="local",
    openai_key=os.getenv("OPENAI_API_KEY"),
    deepgram_key=os.getenv("DEEPGRAM_API_KEY"),
    elevenlabs_key=os.getenv("ELEVENLABS_API_KEY"),
    twilio_sid=os.getenv("TWILIO_ACCOUNT_SID"),
    twilio_token=os.getenv("TWILIO_AUTH_TOKEN"),
    phone_number=os.getenv("TWILIO_PHONE_NUMBER"),
    webhook_url=os.getenv("WEBHOOK_URL"),
)

# ── Define a pipeline agent with custom STT / TTS ───────────────────
agent = phone.agent(
    provider="pipeline",
    stt=Patter.deepgram(api_key=os.getenv("DEEPGRAM_API_KEY"), language="en"),
    tts=Patter.elevenlabs(api_key=os.getenv("ELEVENLABS_API_KEY"), voice="rachel"),
    system_prompt=(
        "You are a friendly startup onboarding assistant. Help new users "
        "set up their account, choose a plan, and understand the product. "
        "Be concise and enthusiastic."
    ),
    voice="rachel",
    first_message="Hey there! Welcome aboard — let me help you get started.",
)


# ── Custom message handler ───────────────────────────────────────────
async def on_message(data: dict) -> str:
    """Process the user's transcribed text and return a response."""
    user_text = data.get("text", "")
    if "pricing" in user_text.lower():
        return (
            "We have three plans: Starter at $29/month, Growth at $99/month, "
            "and Enterprise with custom pricing. Which sounds right for you?"
        )
    # Fall back to the LLM for everything else
    return None


# ── Start the server with recording enabled ──────────────────────────
if __name__ == "__main__":
    asyncio.run(phone.serve(agent, port=8000, recording=True, on_message=on_message))
