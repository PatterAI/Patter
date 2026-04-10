"""Pipeline mode — use YOUR agent to handle conversations.

Bring your own LLM (Claude, GPT, LangChain, or custom logic).
Patter handles STT, TTS, and telephony; your function handles the brain.
"""
import asyncio
from patter import Patter


# Your agent — can be Claude, GPT, LangChain, custom logic, anything.
# Receives a dict with the latest transcript and full history.
# Returns the text the AI should speak.
async def my_agent(data: dict) -> str:
    text = data["text"]
    history = data["history"]  # list of {"role": "user"|"assistant", "text": "..."}

    # Example: Claude via the Anthropic SDK
    # from anthropic import Anthropic
    # client = Anthropic()
    # response = client.messages.create(
    #     model="claude-sonnet-4-20250514",
    #     system="You are a helpful phone assistant.",
    #     messages=[{"role": m["role"], "content": m["text"]} for m in history],
    # )
    # return response.content[0].text

    # Example: OpenAI
    # from openai import OpenAI
    # client = OpenAI()
    # messages = [{"role": "system", "content": "You are a helpful phone assistant."}]
    # messages += [{"role": m["role"], "content": m["text"]} for m in history]
    # response = client.chat.completions.create(model="gpt-4o", messages=messages)
    # return response.choices[0].message.content

    # Simple keyword demo
    print(f"[history={len(history)} turns] user: {text!r}")
    if "hello" in text.lower():
        return "Hi there! I'm running with custom agent logic. How can I help?"
    return f"You said: {text}. I processed this with my own logic, not OpenAI!"


async def main():
    # openai_key is not required in pipeline mode — you provide the LLM yourself
    phone = Patter(
        mode="local",
        twilio_sid="AC...",
        twilio_token="...",
        phone_number="+1...",
        webhook_url="xxx.ngrok-free.dev",
    )

    agent = phone.agent(
        provider="pipeline",
        stt=Patter.deepgram(api_key="dg_..."),
        tts=Patter.elevenlabs(api_key="el_...", voice="aria"),
        language="en",
    )

    print("Listening for calls...")
    await phone.serve(
        agent=agent,
        port=8000,
        on_message=my_agent,  # YOUR function handles every turn
    )


asyncio.run(main())
