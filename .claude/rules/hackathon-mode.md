# Hackathon Mode — Active Until April 12, 2026 6PM PT

This repo is in active hackathon mode. All work prioritizes SHIPPING A DEMO over code quality.

## Override Rules (hackathon only)
- **No TDD.** Test by running the app and making a phone call.
- **No code review.** Ship it.
- **No refactoring.** If it works, don't touch it.
- **No documentation updates.** The code IS the documentation.
- **Hardcode over configure.** If a value can be hardcoded for the demo, hardcode it.
- **Mock over integrate.** Fake data that always works beats real APIs that might fail on stage.

## The Only Test That Matters
Does the phone ring? Does the AI say something smart? Can you have a conversation?

## Two Active Projects
| Project | Path | Track | Port |
|---------|------|-------|------|
| ShipCall | `.hackathon/shipcall/` | FastShot (Mobile) | 8080 |
| VoiceScope | `.hackathon/voicescope/` | AI Studio (Gemini 3) | 8080 |

Both share the same Patter SDK at `sdk/` and the same Twilio/OpenAI credentials.

## Available Commands
- `/hackathon-status` — Check what's working and what's next
- `/test-call` — Trigger a test outbound call
- `/demo-record` — Guide for recording demo videos

## Available Agents
- `hackathon-shipcall` — Builder agent for ShipCall
- `hackathon-voicescope` — Builder agent for VoiceScope
- `rapid-debug` — Fast bug fixing (no investigation frameworks)
- `demo-prep` — One-pagers, video scripts, pitch prep

## Checkpoint Discipline
Every 30 minutes: "Can I demo this right now?" If not, stop adding features and fix what's broken.
