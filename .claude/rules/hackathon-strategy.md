# Hackathon Strategy — Read Before You Code

This rule enforces strategic awareness before any implementation work during the Stanford x DeepMind Hackathon (April 12, 2026). A Claude session that starts coding without understanding platform constraints will build the wrong thing.

## Mandatory Reads (BLOCKING)

Before writing, editing, or generating ANY implementation code, you MUST read these files in order:

1. `.hackathon/00-context.md` — event context, tracks, judging criteria
2. `.hackathon/2026-04-12-office-hours-design.md` — full design doc with architecture, timeline, risk mitigation
3. The project-specific CLAUDE.md for whichever track you are working on:
   - ShipCall: `.hackathon/shipcall/CLAUDE.md`
   - VoiceScope: `.hackathon/voicescope/CLAUDE.md`

Do NOT start implementation until all three files are loaded into context. If the user says "build ShipCall" or "fix VoiceScope," read the files first, then act. No exceptions.

## Platform Constraints (must acknowledge before coding)

These constraints are non-negotiable. Violating any of them means the submission is invalid or the demo fails.

### ShipCall (FastShot Track)
- The mobile app is built INSIDE FastShot.ai using their visual builder. You do NOT write Swift, Kotlin, React Native, or Flutter code. The mobile app is a prop with 2 screens (event feed + "Call Me Now" button). The phone call is the product.
- The backend is a FastAPI server deployed to Cloud Run. The FastShot app POSTs to it.
- The backend uses the Patter SDK to make outbound calls via Twilio/Telnyx.

### VoiceScope (Google AI Studio Track)
- Must use Gemini 3 (`gemini-3-flash-preview` or current model ID — verify in Google AI Studio before using).
- Backend MUST be deployed to Cloud Run. Running locally with ngrok is a fallback, not the plan.
- The web UI is a single HTML page (no framework, no build step) served alongside the FastAPI backend.
- Gemini analyzes the uploaded image, then Patter calls the user with the analysis. Two API calls, not one.

### Both Tracks
- Demo videos are recorded as Playcasts, not regular screen recordings. They must be 1 minute or less.
- Videos upload to YouTube as UNLISTED during recording, then switch to PUBLIC at 2:30 PM submission time.
- Social traction (YouTube likes, shares, views) is a judging criterion tracked for 2 weeks post-event. Share immediately after going public.
- Solo team (Francesco Rosciano). No teammate coordination needed.

## Submission Checklist (must exist before 2:30 PM PT)

### ShipCall (FastShot Track)
- [ ] One-pager (written project summary)
- [ ] Hosted prototype (Cloud Run URL, working)
- [ ] FastShot.ai mobile app (event feed + Call Me Now button)
- [ ] 2-minute team intro video (YouTube, public)
- [ ] 1-minute demo video (YouTube, public)

### VoiceScope (Google AI Studio Track)
- [ ] One-pager (written project summary)
- [ ] Hosted prototype (Cloud Run URL, working)
- [ ] 2-minute team intro video (YouTube, public)
- [ ] 1-minute demo video (YouTube, public)
- [ ] Code repository link

## Critical Path

**The phone must ring before anything else.**

At minute 25 (11:55 AM), if a Patter outbound call does not successfully ring the developer's phone with a coherent AI agent on the other end, STOP ALL OTHER WORK. No FastShot app. No VoiceScope. No UI polish. Debug the outbound call until it works. Nothing else in the entire hackathon matters if the phone doesn't ring.

Timeline checkpoints:
- **0:00-0:25 (11:30-11:55):** Patter outbound call works. Phone rings. AI speaks. You talk back. HARD GATE.
- **0:25-0:50 (11:55-12:20):** ShipCall tools wired (read_logs, get_test_results, suggest_fix). FastAPI endpoint triggers call.
- **0:50-1:15 (12:20-12:45):** FastShot mobile app done. Full ShipCall demo runs end-to-end.
- **1:15-1:45 (12:45-1:15):** VoiceScope built (Gemini vision + Patter callback + HTML form).
- **1:45-2:05 (1:15-1:45):** Polish and rehearse both demos 3x each.
- **2:05-2:25 (1:45-2:05):** Record demo videos + write one-pagers.
- **2:25-2:30 (2:25-2:30):** Submit both tracks. Videos go PUBLIC.

## What NOT To Do

These are banned during hackathon mode. Do not suggest or perform any of these:

- **No TDD.** Test by running the app and calling the phone.
- **No code review.** Ship it.
- **No refactoring.** If it works, move on.
- **No documentation.** The code is the documentation.
- **No premature optimization.** Hardcode everything. Mock everything.
- **No new abstractions.** Copy-paste is fine. Duplicate code is fine.
- **No dependency research.** Use what's already in the repo or hardcode it.
- **No ExecPlans.** The design doc IS the plan. Do not create `docs/YYYY-MM-DD-PLAN-*.md` files.

## Skill and Agent Routing

Use the right tool for the situation:

| Situation | Action |
|-----------|--------|
| Starting ShipCall work | Use `hackathon-shipcall` agent |
| Starting VoiceScope work | Use `hackathon-voicescope` agent |
| Something is broken | Use `rapid-debug` agent (no investigation frameworks, just fix it) |
| Preparing demos, one-pagers, pitch | Use `demo-prep` agent |
| Every 30-minute checkpoint | Run `/hackathon-status` — "Can I demo this right now?" |

If the answer to "Can I demo this right now?" is no, stop adding features and fix what's broken.

## Mock Data Over Real Integrations

For a 3-hour hackathon, reliability beats authenticity. Hardcoded mock data that always returns the right thing is better than a real API integration that might fail on stage. The audience cannot tell the difference. Use mock data for:
- GitHub webhook payloads (ShipCall)
- Log entries and test results (ShipCall tool handlers)
- Any data that isn't the Patter call itself or the Gemini vision response
