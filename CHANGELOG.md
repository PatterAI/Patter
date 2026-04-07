# Changelog

## 0.2.0 (2026-04-03)

### Features
- Three voice modes: OpenAI Realtime, ElevenLabs ConvAI, Pipeline
- Twilio and Telnyx carrier support
- Embedded local mode — no backend needed
- Agent with system prompt, tools, and dynamic variables
- Built-in system tools: transfer_call, end_call
- Call recording via Twilio API
- Answering machine detection + voicemail drop
- DTMF keypad input forwarded to agent
- Conversation history tracking per call
- Mark-based barge-in for natural interruptions
- Webhook retry (3x) with fallback
- Custom TwiML parameters passthrough
- MCP server for Claude Desktop
- Python + TypeScript SDKs with full parity

### Security
- XML escaping for TwiML injection prevention
- API keys as private attributes
- audioop guard for Python 3.13

## 0.1.0 (2026-03-31)

Initial release.
