"""Hermes phone agent — Patter is the voice shell, Hermes is the brain.

A caller dials your number, Patter answers (carrier + STT + turn-taking + TTS),
and every conversation turn is routed to your local Hermes gateway as the LLM.
Hermes stays on loopback (127.0.0.1:8642); only Patter's carrier webhook is
exposed to the internet, via the tunnel.

Run:
    python app.py

Check your setup first with:
    patter hermes doctor
"""

from __future__ import annotations

import os

from getpatter import (
    DeepgramSTT,
    ElevenLabsRestTTS,
    ElevenLabsTTS,
    HermesLLM,
    Patter,
    Twilio,
)

# REST TTS is the safer default for a first PSTN demo: there is no long-lived
# WebSocket that can stall before the first audio frame. Set
# PATTER_ELEVENLABS_TRANSPORT=ws to opt into streaming once the basics work.
if os.environ.get("PATTER_ELEVENLABS_TRANSPORT", "rest").lower() == "ws":
    tts = ElevenLabsTTS.for_twilio()
else:
    tts = ElevenLabsRestTTS.for_twilio()

phone = Patter(
    carrier=Twilio(),                       # TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN
    phone_number=os.environ["PATTER_PHONE_NUMBER"],
    tunnel=True,                            # auto Cloudflare quick-tunnel (local dev)
)

agent = phone.agent(
    system_prompt=(
        "You are Hermes on a live phone call. Keep replies concise, warm, and "
        "spoken-friendly. Avoid markdown, code blocks, long lists, and URLs "
        "unless the caller asks. If you use a tool, say you are checking, then "
        "summarize the result naturally. If interrupted, stop and answer the "
        "latest request."
    ),
    language=os.environ.get("PATTER_LANGUAGE", "en"),
    first_message="Hello, this is Hermes. How can I help?",
    stt=DeepgramSTT(),                      # DEEPGRAM_API_KEY
    llm=HermesLLM(session_key_from="caller_hash"),   # http://127.0.0.1:8642/v1
    tts=tts,                                # ELEVENLABS_API_KEY
    long_turn_message="One moment, let me check that.",
    llm_error_message="Sorry, I'm having trouble reaching Hermes right now.",
)

if __name__ == "__main__":
    phone.serve(agent)                      # answers inbound calls
