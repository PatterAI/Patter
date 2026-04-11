# API Reference

The Patter backend exposes a REST API for account and phone number management, and a WebSocket endpoint used by the SDK.

Base URL: `https://api.getpatter.com` (Patter Cloud) or your self-hosted URL.

## Authentication

All REST endpoints require an API key in the `X-API-Key` header:

```
X-API-Key: pt_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

The WebSocket endpoint also uses `X-API-Key` in the connection headers.

---

## Accounts

### POST /api/accounts

Create a new account and get an API key.

**No authentication required.**

**Request:**

```
POST /api/accounts
Content-Type: application/json
```

```json
{
  "email": "you@example.com"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `email` | `string` | Yes | A valid email address. Must be unique. |

**Response — 201 Created:**

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "email": "you@example.com",
  "api_key": "pt_your_api_key_here"
}
```

| Field | Type | Description |
|---|---|---|
| `id` | `string (UUID)` | Account identifier |
| `email` | `string` | Registered email |
| `api_key` | `string` | API key — store this securely |

**Error responses:**

| Code | Meaning |
|---|---|
| `409 Conflict` | Email already registered |
| `422 Unprocessable Entity` | Invalid email format |

---

### GET /api/accounts/me

Get the current account's details.

**Request:**

```
GET /api/accounts/me
X-API-Key: pt_xxx
```

**Response — 200 OK:**

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "email": "you@example.com",
  "api_key": "pt_your_api_key_here"
}
```

**Error responses:**

| Code | Meaning |
|---|---|
| `401 Unauthorized` | Missing or invalid API key |

---

## Phone Numbers

### POST /api/phone-numbers

Register an existing phone number with Patter.

Use this when you already own a phone number on Twilio or Telnyx and want to point it at your Patter handler. For auto-provisioning a new number, see [POST /api/phone-numbers/provision](#post-apiphonumbersprovision).

**Request:**

```
POST /api/phone-numbers
Content-Type: application/json
X-API-Key: pt_xxx
```

```json
{
  "number": "+14155550000",
  "provider": "telnyx",
  "provider_credentials": {
    "api_key": "KEY4...",
    "connection_id": "your-telnyx-connection-id"
  },
  "country": "US",
  "stt_config": {
    "provider": "deepgram",
    "api_key": "dg_...",
    "language": "en"
  },
  "tts_config": {
    "provider": "elevenlabs",
    "api_key": "el_...",
    "voice": "rachel"
  }
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `number` | `string` | Yes | Phone number in E.164 format |
| `provider` | `string` | Yes | `"twilio"` or `"telnyx"` |
| `provider_credentials` | `object` | Yes | Provider-specific credentials (see below) |
| `country` | `string` | Yes | ISO 3166-1 alpha-2 country code |
| `stt_config` | `object` | No | STT configuration. If omitted, uses backend defaults. |
| `tts_config` | `object` | No | TTS configuration. If omitted, uses backend defaults. |

**Provider credentials:**

For Twilio:
```json
{
  "api_key": "AC...",
  "api_secret": "your_auth_token"
}
```

For Telnyx:
```json
{
  "api_key": "KEY4...",
  "connection_id": "your-telnyx-app-connection-id"
}
```

**STT config options:**

```json
{ "provider": "deepgram", "api_key": "dg_...", "language": "en" }
{ "provider": "whisper",  "api_key": "sk-...", "language": "en" }
{ "provider": "openai_realtime", "api_key": "sk-..." }
{ "provider": "elevenlabs_convai", "api_key": "el_..." }
```

**TTS config options:**

```json
{ "provider": "elevenlabs", "api_key": "el_...", "voice": "rachel" }
{ "provider": "openai",     "api_key": "sk-...", "voice": "alloy" }
```

**Response — 201 Created:**

```json
{
  "id": "661f9511-f39c-52e5-b827-557766551111",
  "number": "+14155550000",
  "provider": "telnyx",
  "country": "US",
  "status": "active",
  "stt_config": { "provider": "deepgram", "language": "en" },
  "tts_config": { "provider": "elevenlabs", "voice": "rachel" }
}
```

Note: `api_key` fields are stripped from the response. Credentials are stored encrypted.

**Error responses:**

| Code | Meaning |
|---|---|
| `401 Unauthorized` | Invalid API key |
| `409 Conflict` | Number already registered |
| `422 Unprocessable Entity` | Invalid provider or missing fields |

---

### POST /api/phone-numbers/provision

Auto-provision a new phone number from a telephony provider.

Patter calls the provider's API to purchase a number in the given country, then registers it on your account.

**Request:**

```
POST /api/phone-numbers/provision
Content-Type: application/json
X-API-Key: pt_xxx
```

```json
{
  "provider": "telnyx",
  "provider_credentials": {
    "api_key": "KEY4..."
  },
  "country": "US",
  "stt_config": {
    "provider": "deepgram",
    "api_key": "dg_...",
    "language": "en"
  },
  "tts_config": {
    "provider": "elevenlabs",
    "api_key": "el_...",
    "voice": "rachel"
  }
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `provider` | `string` | Yes | `"twilio"` or `"telnyx"` |
| `provider_credentials` | `object` | Yes | Provider credentials with purchase rights |
| `country` | `string` | Yes | ISO 3166-1 alpha-2 country code |
| `stt_config` | `object` | No | STT configuration |
| `tts_config` | `object` | No | TTS configuration |

**Response — 201 Created:** Same as POST /api/phone-numbers.

**Error responses:**

| Code | Meaning |
|---|---|
| `400 Bad Request` | Unknown provider |
| `401 Unauthorized` | Invalid API key |
| `502 Bad Gateway` | Provider API error (e.g. no numbers available) |

---

### GET /api/phone-numbers

List all phone numbers on the current account.

**Request:**

```
GET /api/phone-numbers
X-API-Key: pt_xxx
```

**Response — 200 OK:**

```json
[
  {
    "id": "661f9511-f39c-52e5-b827-557766551111",
    "number": "+14155550000",
    "provider": "telnyx",
    "country": "US",
    "status": "active",
    "stt_config": { "provider": "deepgram", "language": "en" },
    "tts_config": { "provider": "elevenlabs", "voice": "rachel" }
  }
]
```

Returns an empty array if no numbers are registered.

---

## Health check

### GET /health

Returns the server status. No authentication required.

**Response — 200 OK:**

```json
{ "status": "ok" }
```

---

## Webhooks — Twilio

These endpoints are called by Twilio and are not intended to be called directly by your application.

### POST /webhooks/twilio/recording

Twilio recording status callback. Called by Twilio when a call recording is available or when its status changes.

Triggered automatically when `recording=True` is passed to `phone.serve()`.

**Request (form-encoded, from Twilio):**

| Field | Description |
|---|---|
| `CallSid` | Twilio call SID |
| `RecordingSid` | Unique identifier for the recording |
| `RecordingUrl` | URL of the recording audio file (MP3/WAV) |
| `RecordingStatus` | `"completed"`, `"failed"`, or `"absent"` |
| `RecordingDuration` | Duration in seconds |
| `RecordingChannels` | Number of audio channels |
| `RecordingStartTime` | ISO 8601 start time |

**Response — 204 No Content**

---

### POST /webhooks/twilio/amd

Twilio Answering Machine Detection (AMD) result callback. Called by Twilio after AMD completes on an outbound call.

Triggered automatically when `machine_detection=True` (Python) or `machineDetection: true` (TypeScript) is passed to `phone.call()`.

**Request (form-encoded, from Twilio):**

| Field | Description |
|---|---|
| `CallSid` | Twilio call SID |
| `AnsweredBy` | `"human"`, `"machine_start"`, `"machine_end_beep"`, `"machine_end_silence"`, `"machine_end_other"`, `"fax"`, or `"unknown"` |
| `MachineDetectionDuration` | Time taken for detection (ms) |

If `AnsweredBy` indicates a machine, Patter plays the configured `voicemail_message` and hangs up. If it indicates a human, the agent proceeds normally.

**Response — 204 No Content**

---

## WebSocket — SDK connection

### WS /ws/sdk

The SDK connects to this endpoint to receive call events and send responses. You do not call this directly — the SDK handles it.

**Connection:**

```
WebSocket: wss://api.getpatter.com/ws/sdk
Header: X-API-Key: pt_xxx
```

Authentication happens at connection time. If the key is invalid, the server closes with code `4001`.

---

### Server → Client messages

Messages the server sends to the connected SDK.

**message** — Caller spoke:

```json
{
  "type": "message",
  "call_id": "abc123",
  "text": "Hi, I have a question about my order.",
  "caller": "+14155551234",
  "history": [
    { "role": "user", "text": "Hi, I have a question about my order.", "timestamp": 1712345678000 }
  ]
}
```

`history` contains the full conversation so far, including the current utterance.

**call_start** — Call began:

```json
{
  "type": "call_start",
  "call_id": "abc123",
  "caller": "+14155551234",
  "callee": "+14155550000",
  "mode": "pipeline",
  "custom_params": {
    "customer_id": "cust_789",
    "plan": "pro"
  }
}
```

`mode` values: `"pipeline"`, `"realtime"`, `"elevenlabs_convai"`.

`custom_params` contains TwiML `<Parameter>` values set on your inbound call flow. Empty object if none are set.

**call_end** — Call ended:

```json
{
  "type": "call_end",
  "call_id": "abc123",
  "duration_seconds": 142
}
```

**call_initiated** — Outbound call placed:

```json
{
  "type": "call_initiated",
  "call_id": "abc123",
  "to": "+14155551234",
  "from": "+14155550000",
  "first_message": "Hi! I'm calling about your order."
}
```

**dtmf** — Caller pressed a key:

```json
{
  "type": "dtmf",
  "call_id": "abc123",
  "digit": "1"
}
```

Sent when the caller presses a key during the call. The SDK also synthesises a `message` event with `text = "[DTMF: 1]"` so your `on_message` handler receives it uniformly.

**mark** — Audio playback position marker:

```json
{
  "type": "mark",
  "call_id": "abc123",
  "mark_name": "chunk_7"
}
```

Sent after each audio chunk is queued for playback. Used internally to implement precise barge-in: when the caller speaks, audio is cleared from the last acknowledged mark. You do not need to handle this event in application code.

**error** — An error occurred:

```json
{
  "type": "error",
  "message": "No phone numbers registered"
}
```

---

### Client → Server messages

Messages the SDK sends to the server.

**response** — Your handler's reply:

```json
{
  "type": "response",
  "call_id": "abc123",
  "text": "Sure, I can help with that. What is your order number?"
}
```

Sent automatically by the SDK after your `on_message` handler returns.

**call** — Place an outbound call:

```json
{
  "type": "call",
  "to": "+14155551234",
  "from": "+14155550000",
  "first_message": "Hi, this is a reminder about your appointment.",
  "machine_detection": true,
  "voicemail_message": "Hi, please call us back."
}
```

| Field | Required | Description |
|---|---|---|
| `to` | Yes | Destination E.164 number |
| `from` | No | Caller ID; uses first registered number if empty |
| `first_message` | No | AI greeting when callee answers |
| `machine_detection` | No | Enable AMD. Defaults to `false`. |
| `voicemail_message` | No | Played when AMD detects a machine. |

---

## System Tools

These tools are automatically available to every agent at runtime. They are injected by Patter and do not need to be declared in your `tools` array.

### transfer_call

Transfers the active call to a different phone number via the Twilio REST API.

```json
{
  "name": "transfer_call",
  "description": "Transfer the current call to a different phone number.",
  "parameters": {
    "type": "object",
    "properties": {
      "to": {
        "type": "string",
        "description": "Destination phone number in E.164 format."
      }
    },
    "required": ["to"]
  }
}
```

The agent invokes this tool autonomously based on conversation context. No explicit trigger code is required.

**Internal behaviour:** Patter calls the Twilio REST API to redirect the call, then closes the agent session.

### end_call

Hangs up the active call via the Twilio REST API.

```json
{
  "name": "end_call",
  "description": "End the current phone call.",
  "parameters": {
    "type": "object",
    "properties": {}
  }
}
```

The agent invokes this tool after a natural conversation conclusion (e.g. saying goodbye). No explicit trigger code is required.

**Internal behaviour:** Patter calls the Twilio REST API to hang up, then finalises the `CallLog` record with duration.

---

## WebSocket close codes

| Code | Meaning |
|---|---|
| `4001` | Invalid or missing API key |
| `1000` | Normal closure |
| `1006` | Abnormal closure (network error) |
