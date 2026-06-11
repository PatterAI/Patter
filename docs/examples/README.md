# Patter Examples

Runnable examples for both SDKs. Each example ships as a Python / TypeScript
pair with identical behavior.

Run the Python examples with `pip install getpatter` + `python <file>.py`,
and the TypeScript examples with `npm install getpatter` + `npx tsx <file>.ts`.
Each file's header comment lists the environment variables it needs
(`TWILIO_ACCOUNT_SID`, `OPENAI_API_KEY`, ...).

## Getting started

| Example | Files | What it shows |
|---------|-------|---------------|
| Basic inbound | [`basic-inbound.py`](./basic-inbound.py) / [`basic-inbound.ts`](./basic-inbound.ts) | Answer incoming calls with OpenAI Realtime. |
| Basic outbound | [`basic-outbound.py`](./basic-outbound.py) / [`basic-outbound.ts`](./basic-outbound.ts) | Place an outbound call. |
| Local agent | [`local-agent.py`](./local-agent.py) / [`local-agent.ts`](./local-agent.ts) | Run the embedded server in local mode. |

## Voice & conversation

| Example | Files | What it shows |
|---------|-------|---------------|
| Custom voice | [`custom-voice.py`](./custom-voice.py) / [`custom-voice.ts`](./custom-voice.ts) | Pick voices and TTS providers. |
| Dynamic variables | [`dynamic-variables.py`](./dynamic-variables.py) / [`dynamic-variables.ts`](./dynamic-variables.ts) | Template variables in the system prompt. |
| Conversation history | [`conversation-history.py`](./conversation-history.py) / [`conversation-history.ts`](./conversation-history.ts) | Access the per-call transcript history. |
| Pipeline custom agent | [`pipeline-custom-agent.py`](./pipeline-custom-agent.py) / [`pipeline-custom-agent.ts`](./pipeline-custom-agent.ts) | Bring your own LLM in pipeline mode. |

## Turn-taking & latency (pipeline mode)

| Example | Files | What it shows |
|---------|-------|---------------|
| Pause-resume barge-in | [`pause-resume-barge-in.py`](./pause-resume-barge-in.py) / [`pause-resume-barge-in.ts`](./pause-resume-barge-in.ts) | `barge_in_mode="pause_resume"` — pause TTS on interruption and resume after coughs / line noise instead of cancelling the turn. |
| Preemptive generation | [`preemptive-generation.py`](./preemptive-generation.py) / [`preemptive-generation.ts`](./preemptive-generation.ts) | `preemptive_generation=True` — start LLM+TTS on a confident interim transcript and release the audio when the final matches. |
| Smart-turn detection | [`smart-turn-detection.py`](./smart-turn-detection.py) / [`smart-turn-detection.ts`](./smart-turn-detection.ts) | `turn_detector=SmartTurnDetector.load(...)` — semantic end-of-utterance detection so mid-sentence pauses don't cut the caller off. |

## Telephony features

| Example | Files | What it shows |
|---------|-------|---------------|
| Tool calling | [`tool-calling.py`](./tool-calling.py) / [`tool-calling.ts`](./tool-calling.ts) | Define tools the agent can invoke mid-call. |
| Call transfer | [`call-transfer.py`](./call-transfer.py) / [`call-transfer.ts`](./call-transfer.ts) | Escalate to a human with the built-in `transfer_call` tool. |
| Recording + AMD | [`recording-amd.py`](./recording-amd.py) / [`recording-amd.ts`](./recording-amd.ts) | Carrier-side call recording and answering machine detection. |
