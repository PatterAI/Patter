"""Conversation history — agent remembers the entire call."""
import asyncio
from patter import Patter, Twilio, OpenAIRealtime


async def on_transcript(data: dict) -> None:
    """Called after every turn with the latest text and full history."""
    role = data["role"]
    text = data["text"]
    history = data.get("history", [])
    print(f"[{role}] {text}")
    print(f"  History length: {len(history)} turns")


async def on_call_end(data: dict) -> None:
    """Called when the call finishes. Full transcript is available here."""
    transcript = data.get("transcript", [])
    print("\n=== Call Summary ===")
    for entry in transcript:
        print(f"  {entry['role']}: {entry['text']}")
    print(f"  Total turns: {len(transcript)}")


async def main():
    phone = Patter(
        carrier=Twilio(),                               # TWILIO_* from env
        phone_number="+15550001234",
        webhook_url="xxx.ngrok-free.dev",
    )

    agent = phone.agent(
        engine=OpenAIRealtime(voice="alloy"),           # OPENAI_API_KEY from env
        system_prompt="You are a helpful assistant. "
                      "Reference earlier parts of the conversation when relevant.",
        first_message="Hello! Let's have a conversation. I'll remember everything we discuss.",
    )

    await phone.serve(
        agent=agent,
        port=8000,
        on_transcript=on_transcript,
        on_call_end=on_call_end,
    )


asyncio.run(main())
