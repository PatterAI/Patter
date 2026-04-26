"""Example: expose Patter as a Hermes Agent tool.

Drop this file under ``tools/patter.py`` in a Hermes Agent project — Hermes
auto-discovers any ``tools/*.py`` with a top-level ``registry.register()``.

After Hermes restarts, the LLM sees a ``make_phone_call`` tool it can call
during a conversation. The tool dials a real number, returns the transcript,
and the LLM continues reasoning with the result.

Required env:

    TWILIO_ACCOUNT_SID
    TWILIO_AUTH_TOKEN
    TWILIO_PHONE_NUMBER
    PATTER_WEBHOOK_URL    # stable HTTPS hostname, e.g. agent.example.com
    DEEPGRAM_API_KEY
    GROQ_API_KEY
    ELEVENLABS_API_KEY

Reference:
- Hermes Adding Tools docs: https://hermes-agent.nousresearch.com/docs/developer-guide/adding-tools
- Patter integrations: getpatter.integrations.PatterTool
"""

import os

from getpatter import (
    Patter,
    Twilio,
    DeepgramSTT,
    GroqLLM,
    ElevenLabsTTS,
)
from getpatter.integrations import PatterTool

# Hermes' registry is auto-imported when this file is dropped into the
# tools/ directory of a hermes-agent project.
try:
    from tools.registry import registry  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - this file runs inside Hermes
    registry = None  # type: ignore[assignment]


def build_tool() -> PatterTool:
    """Wire up Patter and return the configured PatterTool."""
    phone = Patter(
        carrier=Twilio(),
        phone_number=os.environ["TWILIO_PHONE_NUMBER"],
        webhook_url=os.environ["PATTER_WEBHOOK_URL"],
    )
    return PatterTool(
        phone=phone,
        agent={
            "stt": DeepgramSTT(),
            "llm": GroqLLM(),  # Groq Llama for sub-2s p95 — voice-grade
            "tts": ElevenLabsTTS(),
        },
        # Tool metadata visible to the orchestrating LLM:
        name="make_phone_call",
        description=(
            "Place a real outbound phone call and run a short conversation. "
            "Use when the user asks you to call someone, schedule via phone, "
            "or otherwise reach a human via voice. Returns the transcript and "
            "the call status."
        ),
        max_duration_sec=180,
        recording=False,
    )


# Auto-register at import time so Hermes' tool discovery picks us up.
if registry is not None:
    _tool = build_tool()
    _tool.register_hermes(registry, toolset="patter")
