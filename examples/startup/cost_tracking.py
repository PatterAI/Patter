"""
Cost Tracking
=============
Track per-call costs across STT, TTS, LLM, and telephony providers.
Essential for startups monitoring unit economics and optimising their
voice AI spend.

Requirements:
    pip install patter python-dotenv

Environment variables (.env):
    OPENAI_API_KEY      - OpenAI API key
    DEEPGRAM_API_KEY    - Deepgram API key
    ELEVENLABS_API_KEY  - ElevenLabs API key
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

# ── Initialise with custom pricing rates (USD per minute / per unit) ─
phone = Patter(
    mode="local",
    openai_key=os.getenv("OPENAI_API_KEY"),
    deepgram_key=os.getenv("DEEPGRAM_API_KEY"),
    elevenlabs_key=os.getenv("ELEVENLABS_API_KEY"),
    twilio_sid=os.getenv("TWILIO_ACCOUNT_SID"),
    twilio_token=os.getenv("TWILIO_AUTH_TOKEN"),
    phone_number=os.getenv("TWILIO_PHONE_NUMBER"),
    webhook_url=os.getenv("WEBHOOK_URL"),
    pricing={
        "deepgram": {"price": 0.005},
        "elevenlabs": {"price": 0.018},
        "openai": {"price": 0.012},
        "twilio": {"price": 0.015},
    },
)

# ── Agent ────────────────────────────────────────────────────────────
agent = phone.agent(
    provider="pipeline",
    stt=Patter.deepgram(api_key=os.getenv("DEEPGRAM_API_KEY"), language="en"),
    tts=Patter.elevenlabs(api_key=os.getenv("ELEVENLABS_API_KEY"), voice="rachel"),
    system_prompt="You are a helpful billing assistant. Keep answers short.",
    first_message="Hi! How can I help with your account today?",
)


# ── Real-time cost updates per turn ─────────────────────────────────
async def on_metrics(metrics: dict) -> None:
    """Fires after each conversation turn with updated metrics."""
    cost = metrics.get("cost", {})
    print(f"  [metrics] Turn cost — STT: ${cost.get('stt', 0):.4f}  "
          f"TTS: ${cost.get('tts', 0):.4f}  LLM: ${cost.get('llm', 0):.4f}")


# ── End-of-call cost report ──────────────────────────────────────────
async def on_call_end(data: dict) -> None:
    """Print a formatted cost breakdown when the call ends."""
    metrics = data.get("metrics", {})
    cost = metrics.get("cost", {})
    duration = metrics.get("duration_seconds", 0)

    print("\n╔══════════════════════════════════════╗")
    print("║         CALL COST REPORT             ║")
    print("╠══════════════════════════════════════╣")
    print(f"║  Duration:    {duration:>6.1f}s               ║")
    print(f"║  STT:         ${cost.get('stt', 0):>7.4f}              ║")
    print(f"║  TTS:         ${cost.get('tts', 0):>7.4f}              ║")
    print(f"║  LLM:         ${cost.get('llm', 0):>7.4f}              ║")
    print(f"║  Telephony:   ${cost.get('telephony', 0):>7.4f}              ║")
    print(f"║  ─────────────────────               ║")
    print(f"║  TOTAL:       ${cost.get('total', 0):>7.4f}              ║")
    print("╚══════════════════════════════════════╝\n")


# ── Start the server ────────────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(
        phone.serve(
            agent,
            port=8000,
            on_metrics=on_metrics,
            on_call_end=on_call_end,
        )
    )
