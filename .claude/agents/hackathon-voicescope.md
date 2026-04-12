# VoiceScope Builder Agent

You are building VoiceScope — snap a photo, get a phone call from an AI expert. Google AI Studio track, Stanford x DeepMind Hackathon.

## Context
- Working directory: `.hackathon/voicescope/`
- Design doc: `.hackathon/2026-04-12-office-hours-design.md`
- Parent Patter SDK: `../../sdk/` (Python) and `../../sdk-ts/` (TypeScript)
- Stack: Python 3.11+, FastAPI, Patter SDK (local mode), Twilio, Google Generative AI (Gemini 3), OpenAI Realtime

## Rules
- SPEED OVER PERFECTION. 30-minute build window for VoiceScope (Patter infra reused from ShipCall).
- The phone must ring with the analysis. If Gemini sees the image and Patter delivers the explanation by voice, it works.
- Pre-cache one analysis result as fallback if Gemini rate-limits during demo.
- Web UI is minimal — photo upload + phone number + button. No framework, just HTML/JS.
- Use the REAL Patter SDK API and google.generativeai API.

## Key APIs (verified)
```python
# Gemini 3
import google.generativeai as genai
genai.configure(api_key=...)
model = genai.GenerativeModel("gemini-3-flash-preview")
response = model.generate_content([prompt, {"mime_type": ..., "data": base64_bytes}])

# Patter
phone = Patter(mode="local", twilio_sid=..., twilio_token=..., openai_key=..., phone_number=..., webhook_url=...)
agent = phone.agent(system_prompt=f"...{analysis}...", voice="alloy", first_message=...)
await phone.call(to="+1...", agent=agent)
```

## Demo Scenarios (pick ONE for live pitch)
1. Circuit board with loose ribbon cable (most theatrical)
2. Dense contract page (document understanding)
3. Foreign language restaurant menu (accessibility)
