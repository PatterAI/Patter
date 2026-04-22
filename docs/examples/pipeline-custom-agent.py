"""Pipeline mode — use YOUR agent to handle conversations.

Bring your own LLM (Claude, GPT, LangChain, or custom logic).
Patter handles STT, TTS, and telephony; your function handles the brain.
"""
import asyncio
from getpatter import Patter, Twilio, DeepgramSTT, ElevenLabsTTS


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
    phone = Patter(
        carrier=Twilio(),                               # TWILIO_* from env
        phone_number="+15550001234",
        webhook_url="xxx.ngrok-free.dev",
    )

    # Pipeline mode: plug an STT and a TTS; the LLM flows through on_message.
    agent = phone.agent(
        stt=DeepgramSTT(),                              # DEEPGRAM_API_KEY from env
        tts=ElevenLabsTTS(voice_id="aria"),             # ELEVENLABS_API_KEY from env
        language="en",
    )

    print("Listening for calls...")
    await phone.serve(
        agent=agent,
        port=8000,
        on_message=my_agent,  # YOUR function handles every turn
    )


asyncio.run(main())
