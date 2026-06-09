"""The OpenClaw agent that is the brain on the line.

One scoped OpenClaw agent powers both directions: app.py serves it to inbound
callers, dialer.py dials it outbound. Patter owns the voice (carrier, STT,
turn-taking, barge-in, TTS); every turn is one POST to the local OpenClaw
gateway's /v1/chat/completions as model "openclaw/<OPENCLAW_AGENT>".
"""

from __future__ import annotations

import os

from getpatter import DeepgramSTT, ElevenLabsRestTTS, ElevenLabsTTS, OpenClawLLM

_DEFAULT_SYSTEM_PROMPT = (
    "You are a phone receptionist. Keep replies concise, warm, and "
    "spoken-friendly. Avoid markdown, code blocks, long lists, and URLs unless "
    "the caller asks. If you use a tool, say you are checking, then summarize the "
    "result naturally. If interrupted, stop and answer the latest request."
)
_DEFAULT_FIRST_MESSAGE = "Hello, thanks for calling. How can I help?"


def build_agent(phone, *, system_prompt=None, first_message=None, agent=None):
    """Build the Patter agent backed by one scoped OpenClaw agent.

    Reads OPENCLAW_AGENT for the target (never the default/master). REST TTS is
    the safer default on PSTN; set PATTER_ELEVENLABS_TRANSPORT=ws for streaming.
    """
    if os.environ.get("PATTER_ELEVENLABS_TRANSPORT", "rest").lower() == "ws":
        tts = ElevenLabsTTS.for_twilio()
    else:
        tts = ElevenLabsRestTTS.for_twilio()

    return phone.agent(
        system_prompt=system_prompt or _DEFAULT_SYSTEM_PROMPT,
        language=os.environ.get("PATTER_LANGUAGE", "en"),
        first_message=first_message or _DEFAULT_FIRST_MESSAGE,
        stt=DeepgramSTT(),                                  # DEEPGRAM_API_KEY
        llm=OpenClawLLM(agent=agent or os.environ.get("OPENCLAW_AGENT", "receptionist")),
        tts=tts,                                            # ELEVENLABS_API_KEY
        long_turn_message="One moment, let me check that.",
        llm_error_message="Sorry, I'm having trouble reaching the system right now.",
    )
