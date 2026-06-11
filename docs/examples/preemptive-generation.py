"""Preemptive generation — pay LLM+TTS latency during the caller's own pause.

Pipeline mode with the built-in LLM loop only. With preemptive_generation=True
the SDK starts the LLM (and sentence-chunked TTS synthesis) EARLY on a
confident interim transcript — one that ends with sentence-final punctuation,
or that has been unchanged for preemptive_min_stable_ms — holding the audio in
memory. If the final transcript matches the speculated interim, the buffered
audio is released to the carrier immediately; if it differs, the speculation
is discarded silently and the turn dispatches normally on the final.
"""
import asyncio

from getpatter import Patter, Twilio, DeepgramSTT, OpenAILLM, ElevenLabsTTS


async def main():
    phone = Patter(
        carrier=Twilio(),                               # TWILIO_* from env
        phone_number="+15550001234",
        webhook_url="xxx.ngrok-free.dev",
    )

    agent = phone.agent(
        stt=DeepgramSTT(),                              # DEEPGRAM_API_KEY from env
        llm=OpenAILLM(),                                # OPENAI_API_KEY from env
        tts=ElevenLabsTTS(voice_id="aria"),             # ELEVENLABS_API_KEY from env
        system_prompt="You are a quick, friendly assistant for Acme Corp. "
                      "Keep replies short.",
        first_message="Hi! Thanks for calling Acme. How can I help?",
        preemptive_generation=True,    # default False — opt in to speculation
        preemptive_min_stable_ms=300,  # default 300 — interim without sentence-final
                                       # punctuation must hold steady this long
    )

    print("Listening for calls...")
    await phone.serve(agent=agent, port=8000)


asyncio.run(main())
