"""
Remote Webhook Handler
======================
Offload conversation logic to an external service. Instead of a local
Python function, pass a URL as ``on_message`` — Patter POSTs each
transcribed utterance to your backend and speaks the response.

This is ideal for microservice architectures where the LLM / business
logic runs on a separate server (e.g., behind a load balancer).

Requirements:
    pip install patter python-dotenv

Environment variables (.env):
    DEEPGRAM_API_KEY     - Deepgram key for speech-to-text
    ELEVENLABS_API_KEY   - ElevenLabs key for text-to-speech
    TWILIO_ACCOUNT_SID   - Twilio account SID
    TWILIO_AUTH_TOKEN    - Twilio auth token
    TWILIO_PHONE_NUMBER  - Your Twilio phone number (E.164 format)
    WEBHOOK_URL          - Public URL where Twilio can reach this server
"""

import asyncio
import os

from dotenv import load_dotenv

load_dotenv()

from patter import Patter

phone = Patter(
    mode="local",
    twilio_sid=os.getenv("TWILIO_ACCOUNT_SID"),
    twilio_token=os.getenv("TWILIO_AUTH_TOKEN"),
    phone_number=os.getenv("TWILIO_PHONE_NUMBER"),
    webhook_url=os.getenv("WEBHOOK_URL"),
)

agent = phone.agent(
    provider="pipeline",
    system_prompt="You are a helpful assistant.",  # Used as fallback context
    stt=Patter.deepgram(api_key=os.environ["DEEPGRAM_API_KEY"]),
    tts=Patter.elevenlabs(api_key=os.environ["ELEVENLABS_API_KEY"], voice="aria"),
    language="en",
)

# ── Remote webhook as on_message ─────────────────────────────────────
#
# When on_message is a URL string, Patter POSTs a JSON payload to it:
#
#   POST https://api.yourcompany.com/patter/message
#   Headers:
#     Content-Type: application/json
#     X-Patter-Signature: sha256=<HMAC-SHA256 of body using your webhook secret>
#
#   Body:
#     {
#       "text": "What are your business hours?",
#       "call_id": "CA1234567890abcdef",
#       "caller": "+15551234567",
#       "history": [
#         {"role": "user", "text": "Hi there"},
#         {"role": "assistant", "text": "Hello! How can I help?"},
#         {"role": "user", "text": "What are your business hours?"}
#       ]
#     }
#
#   Expected response: {"text": "We're open Monday to Friday, 9 AM to 5 PM."}
#
# Verify the X-Patter-Signature header to ensure the request came from
# Patter. Compute HMAC-SHA256 of the raw request body using your webhook
# secret and compare with the header value.
#
# For low-latency streaming, use a WebSocket URL instead:
#   on_message="wss://api.yourcompany.com/patter/stream"
#
# Patter sends the same JSON payload over the WebSocket and reads the
# response as a single text frame.

if __name__ == "__main__":
    print("Listening for calls (remote webhook mode)...")
    asyncio.run(
        phone.serve(
            agent,
            port=8000,
            on_message="https://api.yourcompany.com/patter/message",
        )
    )
