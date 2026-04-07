# Patter

The open-source SDK for voice AI. Connect any AI agent to phone calls with ~10 lines of code.

[![PyPI](https://img.shields.io/pypi/v/patter)](https://pypi.org/project/patter/)
[![npm](https://img.shields.io/npm/v/patter)](https://www.npmjs.com/package/patter)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Tests](https://github.com/PatterAI/Patter/actions/workflows/test.yml/badge.svg)](https://github.com/PatterAI/Patter/actions)

## Quick Start

### Python

```bash
pip install patter
```

```python
import asyncio
from patter import Patter

async def main():
    phone = Patter(
        twilio_sid="AC...", twilio_token="...",
        openai_key="sk-...",
        phone_number="+15550001234",
        webhook_url="abc.ngrok-free.app",
    )
    agent = phone.agent(
        system_prompt="You are a helpful assistant.",
        voice="alloy",
        first_message="Hello! How can I help you today?",
    )
    await phone.serve(agent, port=8000)

asyncio.run(main())
```

### TypeScript

```bash
npm install patter
```

```typescript
import { Patter } from "patter";

const phone = new Patter({
  mode: "local",
  twilioSid: "AC...", twilioToken: "...",
  openaiKey: "sk-...",
  phoneNumber: "+15550001234",
  webhookUrl: "abc.ngrok-free.app",
});

const agent = phone.agent({
  systemPrompt: "You are a helpful assistant.",
  voice: "alloy",
  firstMessage: "Hello! How can I help you today?",
});

await phone.serve({ agent, port: 8000 });
```

## Features

- **Three Voice Modes** -- OpenAI Realtime (lowest latency), Pipeline (Deepgram/Whisper STT + ElevenLabs/OpenAI TTS), ElevenLabs ConvAI
- **Two Carriers** -- Twilio and Telnyx
- **Call Recording** -- Enable with `recording=True`
- **Call Transfer** -- Built-in `transfer_call` tool (auto-injected)
- **DTMF** -- Keypad presses forwarded as `[DTMF: N]`
- **Machine Detection** -- AMD with voicemail drop for outbound calls
- **Dynamic Variables** -- `{name}` placeholders in system prompts and first messages
- **Output Guardrails** -- Block terms or custom check functions
- **Tool Calling** -- Webhook-based function calls during conversation with 3x auto-retry
- **Conversation History** -- Full history available in every callback
- **First Message** -- Agent speaks first when call connects

## Voice Modes

| Mode | Provider | Latency | Best For |
|---|---|---|---|
| **OpenAI Realtime** | OpenAI | Lowest | Fluid, low-latency conversations (default) |
| **Pipeline** | Deepgram/Whisper + ElevenLabs/OpenAI TTS | Low | Independent control over STT and TTS providers |
| **ElevenLabs ConvAI** | ElevenLabs | Low | ElevenLabs-managed conversation flow |

Set via the `provider` parameter on `agent()`: `"openai_realtime"` (default), `"pipeline"`, or `"elevenlabs_convai"`.

## Examples

### Inbound Call (answer incoming)

<table>
<tr><td><strong>Python</strong></td><td><strong>TypeScript</strong></td></tr>
<tr>
<td>

```python
import asyncio
from patter import Patter

async def main():
    phone = Patter(
        twilio_sid="AC...",
        twilio_token="...",
        openai_key="sk-...",
        phone_number="+15550001234",
        webhook_url="abc.ngrok-free.app",
    )
    agent = phone.agent(
        system_prompt="You are a customer service agent.",
        first_message="Thanks for calling! How can I help?",
    )
    await phone.serve(
        agent,
        port=8000,
        on_call_start=lambda d: print(f"Call from {d['caller']}"),
        on_call_end=lambda d: print(f"Call ended, {len(d.get('history', []))} turns"),
    )

asyncio.run(main())
```

</td>
<td>

```typescript
import { Patter } from "patter";

const phone = new Patter({
  mode: "local",
  twilioSid: "AC...",
  twilioToken: "...",
  openaiKey: "sk-...",
  phoneNumber: "+15550001234",
  webhookUrl: "abc.ngrok-free.app",
});

const agent = phone.agent({
  systemPrompt: "You are a customer service agent.",
  firstMessage: "Thanks for calling! How can I help?",
});

await phone.serve({
  agent,
  port: 8000,
  onCallStart: async (d) => console.log(`Call from ${d.caller}`),
  onCallEnd: async (d) => console.log("Call ended"),
});
```

</td>
</tr>
</table>

### Outbound Call (make a call)

<table>
<tr><td><strong>Python</strong></td><td><strong>TypeScript</strong></td></tr>
<tr>
<td>

```python
import asyncio
from patter import Patter

async def main():
    phone = Patter(
        twilio_sid="AC...",
        twilio_token="...",
        openai_key="sk-...",
        phone_number="+15550001234",
        webhook_url="abc.ngrok-free.app",
    )
    agent = phone.agent(
        system_prompt="You are a reminder bot.",
        first_message="Hi! This is a reminder about your appointment tomorrow.",
    )
    await phone.serve(agent, port=8000)
    await phone.call(to="+14155559876", agent=agent)

asyncio.run(main())
```

</td>
<td>

```typescript
import { Patter } from "patter";

const phone = new Patter({
  mode: "local",
  twilioSid: "AC...",
  twilioToken: "...",
  openaiKey: "sk-...",
  phoneNumber: "+15550001234",
  webhookUrl: "abc.ngrok-free.app",
});

const agent = phone.agent({
  systemPrompt: "You are a reminder bot.",
  firstMessage: "Hi! This is a reminder about your appointment tomorrow.",
});

await phone.serve({ agent, port: 8000 });
await phone.call({ to: "+14155559876", agent });
```

</td>
</tr>
</table>

### Pipeline Mode (custom STT + TTS)

<table>
<tr><td><strong>Python</strong></td><td><strong>TypeScript</strong></td></tr>
<tr>
<td>

```python
agent = phone.agent(
    system_prompt="You are a helpful assistant.",
    provider="pipeline",
    stt=Patter.deepgram(api_key="dg_...", language="en"),
    tts=Patter.elevenlabs(api_key="el_...", voice="rachel"),
)

await phone.serve(
    agent,
    port=8000,
    on_message=my_llm_handler,  # your LLM logic
)
```

</td>
<td>

```typescript
const agent = phone.agent({
  systemPrompt: "You are a helpful assistant.",
  provider: "pipeline",
  stt: Patter.deepgram({ apiKey: "dg_...", language: "en" }),
  tts: Patter.elevenlabs({ apiKey: "el_...", voice: "rachel" }),
});

await phone.serve({
  agent,
  port: 8000,
  onMessage: myLlmHandler, // your LLM logic
});
```

</td>
</tr>
</table>

### Tool Calling

<table>
<tr><td><strong>Python</strong></td><td><strong>TypeScript</strong></td></tr>
<tr>
<td>

```python
agent = phone.agent(
    system_prompt="You are a booking assistant.",
    tools=[{
        "name": "check_availability",
        "description": "Check if a date is available",
        "parameters": {
            "type": "object",
            "properties": {
                "date": {"type": "string"},
            },
            "required": ["date"],
        },
        "webhook_url": "https://api.example.com/check",
    }],
)
```

</td>
<td>

```typescript
const agent = phone.agent({
  systemPrompt: "You are a booking assistant.",
  tools: [{
    name: "check_availability",
    description: "Check if a date is available",
    parameters: {
      type: "object",
      properties: {
        date: { type: "string" },
      },
      required: ["date"],
    },
    webhookUrl: "https://api.example.com/check",
  }],
});
```

</td>
</tr>
</table>

Patter POSTs tool arguments as JSON to your `webhook_url` and uses the response as the tool result. Failed requests are retried up to 3 times. The built-in `transfer_call` and `end_call` tools are auto-injected into every agent.

### Dynamic Variables

<table>
<tr><td><strong>Python</strong></td><td><strong>TypeScript</strong></td></tr>
<tr>
<td>

```python
agent = phone.agent(
    system_prompt="You are helping {customer_name}, account #{account_id}.",
    first_message="Hi {customer_name}! How can I help?",
    variables={
        "customer_name": "Jane",
        "account_id": "A-789",
    },
)
```

</td>
<td>

```typescript
const agent = phone.agent({
  systemPrompt: "You are helping {customer_name}, account #{account_id}.",
  firstMessage: "Hi {customer_name}! How can I help?",
  variables: {
    customer_name: "Jane",
    account_id: "A-789",
  },
});
```

</td>
</tr>
</table>

### Guardrails

<table>
<tr><td><strong>Python</strong></td><td><strong>TypeScript</strong></td></tr>
<tr>
<td>

```python
agent = phone.agent(
    system_prompt="You are a customer service agent.",
    guardrails=[
        Patter.guardrail(
            name="no-competitors",
            blocked_terms=["Competitor Inc", "OtherCo"],
            replacement="I can only speak about our products.",
        ),
        Patter.guardrail(
            name="no-profanity",
            check=lambda text: has_profanity(text),
        ),
    ],
)
```

</td>
<td>

```typescript
const agent = phone.agent({
  systemPrompt: "You are a customer service agent.",
  guardrails: [
    Patter.guardrail({
      name: "no-competitors",
      blockedTerms: ["Competitor Inc", "OtherCo"],
      replacement: "I can only speak about our products.",
    }),
    Patter.guardrail({
      name: "no-profanity",
      check: (text) => hasProfanity(text),
    }),
  ],
});
```

</td>
</tr>
</table>

### Call Recording + Machine Detection

<table>
<tr><td><strong>Python</strong></td><td><strong>TypeScript</strong></td></tr>
<tr>
<td>

```python
# Recording: enable on serve()
await phone.serve(agent, port=8000, recording=True)

# AMD + voicemail: enable on call()
await phone.call(
    to="+14155559876",
    agent=agent,
    machine_detection=True,
    voicemail_message="Hi, please call us back at 555-0123.",
)
```

</td>
<td>

```typescript
// Recording: enable on serve()
await phone.serve({ agent, port: 8000, recording: true });

// AMD + voicemail: enable on call()
await phone.call({
  to: "+14155559876",
  agent,
  machineDetection: true,
  voicemailMessage: "Hi, please call us back at 555-0123.",
});
```

</td>
</tr>
</table>

## API Reference

### `Patter()`

Creates a Patter client for local (embedded) mode.

**Python:**

```python
phone = Patter(
    twilio_sid="AC...",       # Twilio Account SID
    twilio_token="...",       # Twilio Auth Token
    openai_key="sk-...",      # OpenAI API key (for Realtime mode)
    phone_number="+1...",     # Your phone number (E.164)
    webhook_url="x.ngrok.app",  # Public hostname (no scheme)
    # Optional — for Telnyx instead of Twilio:
    # telnyx_key="KEY...",
    # telnyx_connection_id="...",
    # Optional — for Pipeline mode:
    # deepgram_key="dg_...",
    # elevenlabs_key="el_...",
)
```

**TypeScript:**

```typescript
const phone = new Patter({
  mode: "local",
  twilioSid: "AC...",
  twilioToken: "...",
  openaiKey: "sk-...",
  phoneNumber: "+1...",
  webhookUrl: "x.ngrok.app",
  // Optional — for Telnyx instead of Twilio:
  // telephonyProvider: "telnyx",
  // telnyxKey: "KEY...",
  // telnyxConnectionId: "...",
});
```

### `patter.agent()`

Creates an agent configuration.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `system_prompt` | `str` | (required) | Instructions for the AI agent. Supports `{variable}` placeholders. |
| `voice` | `str` | `"alloy"` | TTS voice name (e.g., `"alloy"`, `"echo"`, `"rachel"`). |
| `model` | `str` | `"gpt-4o-mini-realtime-preview"` | OpenAI Realtime model ID. |
| `language` | `str` | `"en"` | BCP-47 language code. |
| `first_message` | `str` | `""` | Agent speaks this immediately when a call connects. Supports `{variable}` placeholders. |
| `tools` | `list[dict]` | `None` | Tool definitions with `name`, `description`, `parameters`, and `webhook_url`. |
| `provider` | `str` | `"openai_realtime"` | Voice mode: `"openai_realtime"`, `"pipeline"`, or `"elevenlabs_convai"`. |
| `stt` | `STTConfig` | `None` | STT config for pipeline mode. Use `Patter.deepgram()` or `Patter.whisper()`. |
| `tts` | `TTSConfig` | `None` | TTS config for pipeline mode. Use `Patter.elevenlabs()` or `Patter.openai_tts()`. |
| `variables` | `dict` | `None` | Values substituted into `system_prompt` and `first_message` at call time. |
| `guardrails` | `list` | `None` | Output guardrails created with `Patter.guardrail()`. |

### `patter.serve()`

Starts the embedded server for inbound calls. Blocks until stopped.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `agent` | `Agent` | (required) | Agent configuration from `patter.agent()`. |
| `port` | `int` | `8000` | TCP port to bind to. |
| `recording` | `bool` | `False` | Enable call recording via the telephony provider. |
| `on_call_start` | `callable` | `None` | `async (data) -> None` -- fires when a call connects. |
| `on_call_end` | `callable` | `None` | `async (data) -> None` -- fires when a call ends. Receives `data.history`, `data.transcript`. |
| `on_transcript` | `callable` | `None` | `async (data) -> None` -- fires on each utterance. Receives `data.role`, `data.text`, `data.history`. |
| `on_message` | `callable` | `None` | `async (data) -> str` -- pipeline mode only. Called with user transcript; return value is spoken. |
| `voicemail_message` | `str` | `""` | Spoken when AMD detects a machine (requires `machine_detection=True` on `call()`). |

### `patter.call()`

Makes an outbound call.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `to` | `str` | (required) | Destination phone number in E.164 format. |
| `agent` | `Agent` | (required) | Agent configuration from `patter.agent()`. |
| `machine_detection` | `bool` | `False` | Enable answering machine detection. |
| `voicemail_message` | `str` | `""` | Message played when voicemail is detected. Requires `machine_detection=True`. |

### Provider Helpers

Static methods for configuring STT/TTS providers in pipeline mode.

| Helper | Returns | Parameters |
|---|---|---|
| `Patter.deepgram(api_key, language="en")` | `STTConfig` | Deepgram Nova STT |
| `Patter.whisper(api_key, language="en")` | `STTConfig` | OpenAI Whisper STT |
| `Patter.elevenlabs(api_key, voice="rachel")` | `TTSConfig` | ElevenLabs TTS |
| `Patter.openai_tts(api_key, voice="alloy")` | `TTSConfig` | OpenAI TTS |

### `Patter.guardrail()`

Creates an output guardrail that intercepts AI responses before TTS.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `name` | `str` | (required) | Identifier for logging when the guardrail fires. |
| `blocked_terms` | `list[str]` | `None` | Words/phrases that trigger blocking (case-insensitive). |
| `check` | `callable` | `None` | `(text: str) -> bool` -- return `True` to block. Evaluated after `blocked_terms`. |
| `replacement` | `str` | `"I'm sorry, I can't respond to that."` | What the agent says instead. |

### Callbacks

All callbacks are async functions. The `data` dict includes:

**`on_call_start`**
```python
async def on_call_start(data):
    data["caller"]   # caller phone number
    data["call_id"]  # unique call ID
```

**`on_call_end`**
```python
async def on_call_end(data):
    data["history"]     # [{role: "user"|"assistant", text: "..."}]
    data["transcript"]  # full transcript
    data["duration"]    # call duration in seconds
```

**`on_transcript`**
```python
async def on_transcript(data):
    data["role"]     # "user" or "assistant"
    data["text"]     # transcribed text (includes "[DTMF: N]" for keypad presses)
    data["history"]  # conversation so far
```

**`on_message`** (pipeline mode only)
```python
async def on_message(data) -> str:
    data["text"]     # user's transcribed speech
    data["history"]  # conversation so far
    return "Response to speak"  # return value is sent to TTS
```

## Requirements

- Python 3.11+ or Node.js 18+
- Twilio or Telnyx account
- OpenAI API key (for Realtime mode) or Deepgram + ElevenLabs keys (for Pipeline mode)
- ngrok or similar tunnel for local development (Twilio/Telnyx need a public webhook URL)

## License

MIT -- see [LICENSE](./LICENSE).
