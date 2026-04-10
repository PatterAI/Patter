"""
Custom LLM Integration (Anthropic Claude)
==========================================
Use your own LLM instead of OpenAI Realtime. Patter handles STT, TTS,
and telephony while you control the brain. This example uses Anthropic
Claude via the Messages API, but any LLM works — OpenAI, Mistral,
LLaMA, or your own fine-tuned model.

Requirements:
    pip install patter python-dotenv httpx

Environment variables (.env):
    ANTHROPIC_API_KEY    - Your Anthropic API key
    DEEPGRAM_API_KEY     - Deepgram key for speech-to-text
    ELEVENLABS_API_KEY   - ElevenLabs key for text-to-speech
    TWILIO_ACCOUNT_SID   - Twilio account SID
    TWILIO_AUTH_TOKEN    - Twilio auth token
    TWILIO_PHONE_NUMBER  - Your Twilio phone number (E.164 format)
    WEBHOOK_URL          - Public URL where Twilio can reach this server
"""

import asyncio
import os

import httpx
from dotenv import load_dotenv

load_dotenv()

from patter import Patter

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SYSTEM_PROMPT = (
    "You are a concise, friendly phone assistant for Acme Corp. "
    "Help callers with account questions, billing, and general inquiries. "
    "Keep responses under two sentences — this is a phone call, not a chat."
)


async def on_message(data: dict) -> str:
    """Called each time the caller finishes speaking.

    Patter transcribes speech via Deepgram (STT), passes the text here,
    and synthesises your return value via ElevenLabs (TTS).

    Args:
        data: {"text": str, "history": [{"role": "user"|"assistant", "text": str}],
               "call_id": str, "caller": str}
    """
    history = data["history"]

    # Build the messages payload for Claude
    messages = [{"role": m["role"], "content": m["text"]} for m in history]

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 256,
                "system": SYSTEM_PROMPT,
                "messages": messages,
            },
        )
        response.raise_for_status()
        result = response.json()

    return result["content"][0]["text"]


# ── Initialise Patter in pipeline mode ───────────────────────────────
phone = Patter(
    mode="local",
    twilio_sid=os.getenv("TWILIO_ACCOUNT_SID"),
    twilio_token=os.getenv("TWILIO_AUTH_TOKEN"),
    phone_number=os.getenv("TWILIO_PHONE_NUMBER"),
    webhook_url=os.getenv("WEBHOOK_URL"),
)

# Pipeline mode: Patter does STT + TTS, you provide the LLM
agent = phone.agent(
    provider="pipeline",
    system_prompt=SYSTEM_PROMPT,
    stt=Patter.deepgram(api_key=os.environ["DEEPGRAM_API_KEY"]),
    tts=Patter.elevenlabs(api_key=os.environ["ELEVENLABS_API_KEY"], voice="aria"),
    language="en",
)

# ── Start the server ─────────────────────────────────────────────────
if __name__ == "__main__":
    print("Listening for calls (pipeline mode with Claude)...")
    asyncio.run(phone.serve(agent, port=8000, on_message=on_message))
