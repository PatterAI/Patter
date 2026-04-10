"""Conversation history — agent remembers the entire call."""
import asyncio
from patter import Patter


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
        mode="local",
        twilio_sid="AC...",
        twilio_token="...",
        openai_key="sk-...",
        phone_number="+1...",
        webhook_url="xxx.ngrok-free.dev",
    )

    agent = phone.agent(
        system_prompt="You are a helpful assistant. "
                      "Reference earlier parts of the conversation when relevant.",
        voice="alloy",
        first_message="Hello! Let's have a conversation. I'll remember everything we discuss.",
    )

    await phone.serve(
        agent=agent,
        port=8000,
        on_transcript=on_transcript,
        on_call_end=on_call_end,
    )


asyncio.run(main())
