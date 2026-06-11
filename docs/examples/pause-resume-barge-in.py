"""Pause-and-resume barge-in — survive coughs and line noise (pipeline mode).

With barge_in_mode="pause_resume", a VAD speech_start during the agent's turn
PAUSES playback instead of cancelling it. If a committed final transcript
confirms a real interruption within barge_in_confirm_ms, the turn is cancelled
as usual; if the window expires with no transcript (a cough, background
noise), the agent resumes from the first sentence the caller had not fully
heard — without re-billing TTS.
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
        system_prompt="You are a patient customer-support agent for Acme Corp. "
                      "Answer thoroughly — callers can interrupt you at any time.",
        first_message="Hi! Thanks for calling Acme. How can I help you today?",
        barge_in_mode="pause_resume",  # default "cancel": kill the turn on speech_start
        barge_in_confirm_ms=1500,      # default 1500 — resume TTS if no final
                                       # transcript confirms within this window
    )

    print("Listening for calls...")
    await phone.serve(agent=agent, port=8000)


asyncio.run(main())
