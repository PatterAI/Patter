# Hackathon Ideas -- Stanford x DeepMind, April 12 2026

Sprint: 3 hours. Two tracks. 40+ VCs watching. Up to $5M seed.

This doc contains two polished hackathon ideas built around Patter (AI agents on phone calls with ~10 lines of code). Each idea is designed to produce a working demo in 3 hours that makes judges say "whoa."

---

## Idea 1: ShipCall -- Your Codebase Calls You (FastShot Track)

**One-line pitch:** An AI voice agent that proactively calls you when your code needs attention -- build failures, shipped features, decisions needed -- like having a senior engineer on speed dial who watches your repo 24/7.

### The "Whoa" Moment

Live on stage: the presenter's phone rings mid-pitch. They answer on speaker. A voice says:

> "Hey Francesco, your deploy just landed on production. The chat360 feature branch merged clean, 47 tests passing. But heads up -- there's a new error spike in the auth service, 12 failures in the last 5 minutes. Want me to roll back, or should I dig into the logs first?"

The presenter says "dig into the logs" and the AI reads back the stack trace, identifies the root cause, and suggests a one-line fix. Live. On a phone call. In front of 40 VCs.

That is the demo. A phone that rings when your code needs you, and an AI that actually understands what happened.

### How It Uses Patter

Patter is the entire telephony layer. The core loop:

1. A background watcher monitors git events (commits, CI results, deploy status) via webhooks or polling
2. When something noteworthy happens, it calls `phone.call()` with Patter to ring the developer's phone
3. The AI agent (powered by the Patter `on_message` handler) has full context about the event and can answer follow-up questions
4. Developer can say "roll back," "open a PR," "send me the logs" -- tool calls via Patter's webhook tools execute real actions

Key Patter features used:
- **Outbound calls** (`phone.call(to="+1...", agent=agent)`) -- the AI initiates the call
- **Tool calling** (`Patter.tool()` with local handlers) -- execute git commands, read logs, trigger deploys
- **Dynamic variables** -- inject event context into the system prompt per-call
- **Machine detection + voicemail** -- if you don't answer, it leaves a voicemail summary
- **Conversation history** -- multi-turn debugging over the phone

```python
# The core is ~30 lines
phone = Patter(mode="local", openai_key=..., twilio_sid=..., ...)

agent = phone.agent(
    system_prompt="""You are ShipCall, a senior engineer watching {repo_name}.
    Event: {event_type}
    Details: {event_details}
    Help the developer understand what happened and take action.""",
    voice="alloy",
    first_message="Hey {dev_name}, {event_summary}",
    variables=event_context,
    tools=[
        Patter.tool(name="read_logs", handler=read_logs_handler, ...),
        Patter.tool(name="rollback", handler=rollback_handler, ...),
        Patter.tool(name="get_test_results", handler=test_results_handler, ...),
    ],
)

await phone.call(
    to=dev_phone_number,
    agent=agent,
    machine_detection=True,
    voicemail_message=f"ShipCall update: {event_summary}. Check your dashboard for details.",
)
```

### 3-Hour Build Plan

| Time | Task | Deliverable |
|------|------|-------------|
| 0:00-0:30 | **Patter agent setup** | Working outbound call with hardcoded event context. Phone rings, AI speaks, you can talk back. |
| 0:30-1:00 | **Tool handlers** | 3 tools: `read_logs` (returns mock log data), `get_status` (returns mock CI status), `suggest_fix` (returns a code suggestion). Wire them to the Patter agent. |
| 1:00-1:30 | **Event trigger system** | Simple webhook endpoint (FastAPI, 20 lines) that receives GitHub webhook payloads, parses them, and triggers the outbound call. OR: a polling loop that watches a git repo for new commits. |
| 1:30-2:00 | **FastShot mobile companion** | Build the mobile app in FastShot: a dashboard showing recent ShipCall events, call history, and a "Call Me Now" button. Connect to the same backend via REST. |
| 2:00-2:30 | **Demo prep** | Record the 1-min demo video (phone ringing, conversation, tool execution). Write one-pager. Polish. |

**Critical path:** The first 30 minutes must produce a working phone call. Everything else is enhancement. If Patter + Twilio work, the demo works.

**FastShot mobile app screens:**
1. **Dashboard** -- Recent events (deploy succeeded, test failed, PR merged) with status icons
2. **Call History** -- Past ShipCall conversations with transcripts
3. **Settings** -- Phone number, repo URL, notification preferences
4. **"Call Me Now"** button -- Triggers an immediate status update call

### Why Judges Will Care

| Criterion | How ShipCall Scores |
|-----------|-------------------|
| **Technical feasibility** | Fully working demo. Real phone call, real AI conversation, real tool execution. Patter handles the hard part (telephony). |
| **Innovation** | Nobody is doing proactive AI-to-human phone calls for developer tools. Push notifications are dead -- phone calls cut through. |
| **Real-world value** | Every developer has missed a broken deploy. Every on-call engineer has wished for a smarter pager. This replaces PagerDuty alerts with an AI that actually understands the problem. |
| **Market potential** | $4B+ developer tools market. Every engineering team with CI/CD is a customer. SaaS model: per-call pricing on top of Patter. |
| **Social traction** | "My code called me" is inherently viral. The demo video of a phone ringing during a pitch will get shared. |

### Existing Competition and Differentiation

- **PagerDuty / OpsGenie** -- Send alerts. They tell you something is wrong but don't help you fix it. ShipCall has a conversation.
- **GitHub Copilot** -- Helps you write code. Doesn't call you when it breaks.
- **Linear / Jira notifications** -- Text-based, easy to ignore. Phone calls demand attention.

ShipCall is different because it is *proactive* (AI calls you, not the other way around) and *conversational* (you can ask follow-up questions and take action by voice).

---

## Idea 2: VoiceScope -- Gemini Sees, Patter Speaks (Google AI Studio Track)

**One-line pitch:** Point your phone camera at anything -- a broken machine, a medical bill, a contract, a circuit board -- and a voice AI calls you back to walk you through exactly what you're looking at, what's wrong, and what to do about it.

### The "Whoa" Moment

Live demo: The presenter snaps a photo of a complex circuit board with their phone. Ten seconds later, their phone rings. They answer on speaker:

> "I can see a Raspberry Pi 4 with what looks like a loose ribbon cable on the camera connector -- the ZIF latch isn't fully closed. That's probably why your camera module isn't being detected. To fix it: gently pull the ribbon cable out, make sure the blue side faces the Ethernet port, slide it back in, and press the latch down until it clicks. Want me to walk you through it step by step?"

The audience sees the photo on screen. The AI is describing exactly what's in it -- and offering to help fix it. Over a phone call. No screen needed.

### How It Uses Patter + Gemini 3

This combines two capabilities that don't exist together anywhere:

1. **Gemini 3's multimodal understanding** -- analyzes images with state-of-the-art vision, generates detailed understanding of what it sees
2. **Patter's voice telephony** -- delivers that understanding as a natural phone conversation

The flow:
1. User uploads/takes a photo via the web app (hosted on Cloud Run)
2. Gemini 3 (via Google AI Studio API) analyzes the image and generates a structured analysis: what it sees, what might be wrong, suggested actions
3. The analysis is injected into a Patter agent's system prompt as context
4. Patter calls the user's phone with the analysis, ready for follow-up questions
5. During the call, the user can ask questions -- the agent has Gemini's full analysis in context

```python
# Gemini analyzes the image
import google.generativeai as genai
model = genai.GenerativeModel("gemini-3-flash-preview")
response = model.generate_content([
    "Analyze this image in detail. What do you see? What might be wrong? What should the user do?",
    image_data,
])
analysis = response.text

# Patter calls the user with the analysis
agent = phone.agent(
    system_prompt=f"""You are VoiceScope, an expert visual analyst.
    The user just sent you this image analysis:
    ---
    {analysis}
    ---
    Call them and explain what you found. Be specific and actionable.
    If they ask follow-up questions, answer based on the analysis.""",
    voice="alloy",
    first_message=f"Hey, I just analyzed that image you sent. {one_line_summary}",
)

await phone.call(to=user_phone, agent=agent)
```

### Why Voice + Vision Is Powerful

The key insight: **when your hands are busy, you can't look at a screen.** 

- A mechanic with greasy hands looking at an engine can't type or scroll
- A nurse reading a complex medical document needs both hands free
- A field technician troubleshooting equipment needs to keep their eyes on the machine
- An elderly person trying to understand a confusing bill doesn't want to navigate an app

Voice is the most natural interface for getting expert guidance while your hands and eyes are occupied. Gemini provides the expert knowledge. Patter delivers it as a phone call.

### 3-Hour Build Plan

| Time | Task | Deliverable |
|------|------|-------------|
| 0:00-0:30 | **Gemini vision pipeline** | Python script: send an image to Gemini 3, get structured analysis back. Test with 3-4 sample images. Deploy as a Cloud Run endpoint. |
| 0:30-1:00 | **Patter callback agent** | Working outbound call that reads back Gemini's analysis. Phone rings, AI explains what it saw, handles follow-ups. |
| 1:00-1:30 | **Web upload interface** | Simple web page (HTML + JS, hosted on Cloud Run): take/upload photo, enter phone number, click "Analyze and Call Me." Triggers the Gemini + Patter pipeline. |
| 1:30-2:00 | **Vertical demos** | Prepare 3 compelling demo scenarios: (1) hardware troubleshooting (circuit board), (2) document understanding (medical bill), (3) food/nutrition (photo of a meal). Each shows a different use case. |
| 2:00-2:30 | **Demo prep** | Record the 1-min demo video. Write one-pager. Test the full pipeline end-to-end 3 times. |

**Critical path:** Gemini vision works out of the box. Patter outbound calls work out of the box. The integration is a 50-line Python script. The risk is low.

### Why Judges Will Care

| Criterion | How VoiceScope Scores |
|-----------|---------------------|
| **Technical feasibility** | Both Gemini vision and Patter telephony are production APIs. The glue code is minimal. Working demo guaranteed. |
| **Innovation** | Nobody combines multimodal AI vision with proactive phone calls. This is a genuinely new interaction pattern: snap a photo, get a phone call from an expert. |
| **Real-world value** | Accessibility play: helps anyone who can't easily use a screen (elderly, disabled, hands-busy workers). Solves a real problem for field workers, healthcare, home repair. |
| **Market potential** | Horizontal platform (any visual domain). Vertical SaaS for healthcare, field services, insurance claims, home repair. B2B and B2C. |
| **Social traction** | "I took a photo of my broken dishwasher and an AI called me to tell me how to fix it" -- inherently shareable. |

### Existing Competition and Differentiation

- **Google Lens** -- Identifies objects. Doesn't explain or troubleshoot. No voice.
- **ChatGPT Vision** -- Analyzes images in chat. Requires typing. No phone call.
- **Be My Eyes (with GPT-4)** -- Connects blind users to AI for visual assistance via app. Requires the app open on screen.

VoiceScope is different because the output is a *phone call*, not a chat response. You take one action (snap a photo), and the expert calls you. Zero screen interaction needed after the initial photo.

---

## Recommendation: Build Both. Lead with ShipCall.

**Submit ShipCall to the FastShot track and VoiceScope to the Google AI Studio track.** They share the Patter backend, so 60% of the infrastructure is the same.

### Why ShipCall is the lead:

1. **The demo is unforgettable.** A phone ringing during a pitch is theatrical. It shows the proactive AI pattern in real time. No other team will have their prototype interrupt their own presentation.
2. **Developer tools VCs will get it immediately.** 40 VCs in the room -- many have funded dev tools. "AI PagerDuty that actually helps you fix the problem" is a pitch that lands in one sentence.
3. **The FastShot mobile companion** gives it mobile-native polish, which is literally what the FastShot track is looking for.

### Why VoiceScope is the strong second entry:

1. **Uses Gemini 3**, which is what the Google AI Studio track requires
2. **Different audience appeal** -- accessibility, healthcare, field services. Broader market story.
3. **Lower risk** -- both Gemini vision and Patter calls are well-documented, production APIs. The integration is simple.

### Shared Infrastructure

Both projects use the same Patter local-mode setup:
- Same Twilio account and phone number
- Same ngrok tunnel
- Same Patter SDK installation
- Same outbound call pattern

The only differences are the trigger (GitHub webhook vs. image upload) and the system prompt context (code event vs. image analysis).

### Time Allocation (if building both)

| Time | ShipCall (FastShot) | VoiceScope (AI Studio) |
|------|--------------------|-----------------------|
| 0:00-0:30 | Patter agent + outbound call working | -- |
| 0:30-1:00 | Tool handlers + event triggers | Gemini vision pipeline |
| 1:00-1:30 | FastShot mobile app | Patter callback agent + web UI |
| 1:30-2:00 | Polish + demo scenarios | Polish + demo scenarios |
| 2:00-2:30 | Record demos + submit both | Record demos + submit both |

This works because the Patter setup from 0:00-0:30 is shared. Once you have a working outbound call, both projects are just "different context fed to the same pattern."

### One More Thing: The Pitch Framing

Don't pitch two separate products. Pitch **Patter as a platform** with two proof points:

> "We built two products today, both powered by the same 10-line Patter integration. ShipCall monitors your code and calls you when something breaks. VoiceScope analyzes photos and calls you with expert guidance. Same platform, two verticals, built in 3 hours. That's the power of giving AI agents a phone number."

This positions you as a platform play, not a single-product demo. VCs love platform plays.

---

## Appendix: Technical Prerequisites

### Accounts Needed
- Twilio account with a phone number (free trial works, ~$1 for a number)
- OpenAI API key with Realtime access (for Patter's voice mode)
- Google AI Studio API key (for Gemini 3 -- `gemini-3-flash-preview`)
- ngrok account (free tier, for exposing local server to Twilio)
- FastShot.ai account (for the mobile app)

### Environment Variables
```
OPENAI_API_KEY=sk-...
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...
TWILIO_PHONE_NUMBER=+1...
WEBHOOK_URL=abc.ngrok-free.dev
GOOGLE_AI_STUDIO_KEY=...
```

### Pre-Hackathon Setup (do this TONIGHT)
1. `pip install patter python-dotenv google-generativeai httpx`
2. Set up Twilio phone number and verify it works
3. Run `ngrok http 8000` and note the URL
4. Test a basic Patter outbound call to your own phone (use `examples/developer/basic_outbound.py` as template)
5. Test Gemini 3 vision with a sample image
6. Prepare 3 demo images for VoiceScope (circuit board, medical bill, meal photo)
7. Write the FastShot mobile app screens in advance (dashboard, call history, settings)

If the Patter outbound call works tonight, you're 80% done. Everything else is context and polish.

---

## Appendix: Demo Script (60 seconds)

### ShipCall Demo (FastShot Track)

**[0-10s]** "ShipCall watches your codebase and calls you when something needs attention."

**[10-20s]** Show the FastShot mobile app dashboard. "Here's our mobile companion -- recent events, call history, and a Call Me Now button."

**[20-25s]** Trigger an event (push a commit that breaks tests, or press "Call Me Now").

**[25-40s]** Phone rings. Answer on speaker. AI says: "Hey, your latest push broke 3 tests in the auth module. The error is a missing environment variable REDIS_URL in the test config. Want me to suggest a fix?"

**[40-50s]** Developer says: "Yeah, suggest a fix." AI responds with the specific fix.

**[50-60s]** "ShipCall. Your code has a voice. Built with Patter."

### VoiceScope Demo (AI Studio Track)

**[0-10s]** "VoiceScope: snap a photo, get a phone call from an expert."

**[10-20s]** Take a photo of something on the desk (a circuit board, a receipt, a pill bottle).

**[20-25s]** Upload to web UI, enter phone number, click "Analyze."

**[25-45s]** Phone rings. Answer on speaker. AI describes exactly what it sees in the photo and offers specific guidance.

**[45-55s]** Ask a follow-up question. AI answers with detail from the image analysis.

**[55-60s]** "VoiceScope. See anything. Hear everything. Built with Gemini 3 + Patter."
