#!/usr/bin/env python
"""Place one test outbound call through the OpenClaw voice shell.

    python scripts/test_outbound_call.py +15557654321

The callee picks up and talks to the OpenClaw agent. Requires the same env as
app.py / dialer.py.
"""

from __future__ import annotations

import asyncio
import os
import sys

from getpatter import (
    DeepgramSTT,
    ElevenLabsRestTTS,
    OpenClawLLM,
    Patter,
    Twilio,
)


async def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python scripts/test_outbound_call.py <+E164>")
        return 2
    to = sys.argv[1]
    phone = Patter(
        carrier=Twilio(),
        phone_number=os.environ.get("PATTER_FROM_NUMBER")
        or os.environ["PATTER_PHONE_NUMBER"],
        tunnel=True,
    )
    agent = phone.agent(
        system_prompt=(
            "You are on a short test call. Greet the person warmly and ask how "
            "they are. Keep it brief and spoken-friendly."
        ),
        first_message="Hi, this is a Patter and OpenClaw test call.",
        stt=DeepgramSTT(),
        llm=OpenClawLLM(agent=os.environ.get("OPENCLAW_AGENT", "sales")),
        tts=ElevenLabsRestTTS.for_twilio(),
    )
    result = await phone.call(to, agent=agent, wait=True)
    print(f"Call outcome: {result.outcome if result else 'unknown'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
