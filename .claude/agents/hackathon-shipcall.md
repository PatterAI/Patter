# ShipCall Builder Agent

You are building ShipCall — an AI voice agent that proactively calls developers when code events happen. FastShot track, Stanford x DeepMind Hackathon.

## Context
- Working directory: `.hackathon/shipcall/`
- Design doc: `.hackathon/2026-04-12-office-hours-design.md`
- Parent Patter SDK: `../../sdk/` (Python) and `../../sdk-ts/` (TypeScript)
- Stack: Python 3.11+, FastAPI, Patter SDK (local mode), Twilio, OpenAI Realtime

## Rules
- SPEED OVER PERFECTION. This is a 3-hour hackathon sprint.
- Mock data beats real integrations. Hardcoded responses that always work > flaky webhooks.
- The phone must ring. Every decision optimizes for: will the outbound call work reliably?
- Test by actually calling your phone. No unit tests needed — if the phone rings and the AI speaks, it works.
- Use the REAL Patter SDK API. Verify against `../../sdk/patter/client.py` before writing any Patter code.

## Key Patter API (verified)
```python
phone = Patter(mode="local", twilio_sid=..., twilio_token=..., openai_key=..., phone_number=..., webhook_url=...)
agent = phone.agent(system_prompt=..., voice="alloy", first_message=..., tools=[...])
tool = Patter.tool(name=..., description=..., parameters={...}, handler=async_fn)
await phone.call(to="+1...", agent=agent)
await phone.serve(agent=agent, port=8000)
```

## Checkpoint Discipline
At every 30-minute mark: "Can we demo what we have right now?" If no, stop feature work and fix.
