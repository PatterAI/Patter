# Architecture

This document describes how Patter's components fit together, how a call flows through the system, and how the data model is structured.

## System overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Your Application                            │
│                                                                     │
│   phone = Patter(api_key="pt_xxx")                                  │
│   await phone.connect(on_message=handler)                           │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ WebSocket (wss://api.patter.dev/ws/sdk)
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        Patter Backend                               │
│                                                                     │
│  FastAPI (REST + WebSocket)                                         │
│  ┌────────────────┐  ┌──────────────────┐  ┌─────────────────────┐ │
│  │  SDK WebSocket │  │  Stream WebSocket │  │  REST API           │ │
│  │  /ws/sdk       │  │  /ws/stream/*     │  │  /api/accounts      │ │
│  │                │  │                  │  │  /api/phone-numbers  │ │
│  └────────┬───────┘  └────────┬─────────┘  └─────────────────────┘ │
│           │                   │                                     │
│           ▼                   ▼                                     │
│  ┌──────────────────────────────────────┐                          │
│  │          Call Orchestrator           │                          │
│  │  - Manages active call sessions      │                          │
│  │  - Routes audio to STT provider      │                          │
│  │  - Routes text to SDK handler        │                          │
│  │  - Routes response to TTS provider   │                          │
│  └──────────────────────────────────────┘                          │
│           │                                                         │
│  ┌────────┴─────────────────────────────────────────┐              │
│  │                    PostgreSQL                     │              │
│  │   accounts | phone_numbers | call_logs            │              │
│  └──────────────────────────────────────────────────┘              │
└──────┬───────────────────────────────────────┬──────────────────────┘
       │ Webhooks                               │ Audio stream
       ▼                                        ▼
┌──────────────────┐                ┌───────────────────────────────┐
│  Telephony       │                │  Voice Providers              │
│  ┌─────────────┐ │                │  ┌──────────────┐             │
│  │   Twilio    │ │                │  │  Deepgram     │ STT        │
│  └─────────────┘ │                │  ├──────────────┤             │
│  ┌─────────────┐ │                │  │  Whisper      │ STT        │
│  │   Telnyx    │ │                │  ├──────────────┤             │
│  └─────────────┘ │                │  │  ElevenLabs   │ TTS        │
└──────────────────┘                │  ├──────────────┤             │
         │                          │  │  OpenAI TTS   │ TTS        │
         │                          │  ├──────────────┤             │
         └──── Phone Call ──────────►  │  OpenAI RT    │ STT+TTS    │
                                    │  └──────────────┘             │
                                    └───────────────────────────────┘
```

## Components

### Patter SDK (Python or TypeScript)

The SDK is a thin client. It:

1. Opens a WebSocket to `/ws/sdk` with the API key in the headers.
2. Listens for `message`, `call_start`, and `call_end` events.
3. Calls your `on_message` handler for each `message` event.
4. Sends the handler's return value back as a `response` message.
5. In self-hosted mode, calls the REST API to register the phone number before connecting.

The SDK does not process audio. It only exchanges text with the backend.

### Patter Backend

A FastAPI application with two types of WebSocket endpoints and a REST API.

**SDK WebSocket (`/ws/sdk`):**
- One connection per SDK client
- Authenticates via `X-API-Key` header
- Maintains a map of phone numbers → WebSocket connections
- Forwards transcribed text to the SDK and routes SDK responses back to the call orchestrator

**Stream WebSocket (`/ws/stream/*`):**
- One connection per active phone call
- Opened by the telephony provider (Twilio or Telnyx) when a call starts
- Receives raw audio frames from the caller
- Streams audio to the configured STT provider
- Routes STT output to the call orchestrator

**Call Orchestrator:**
- Created per call session
- Coordinates the pipeline: audio in → STT → SDK handler → TTS → audio out
- Manages the `first_message` for outbound calls (speaks it before waiting for the caller)
- Records calls to `call_logs`

**REST API:**
- Account management (create, get)
- Phone number management (register, provision, list)
- All credentials are encrypted before storage using Fernet-based encryption

### Telephony providers

**Twilio:**
- Webhook at `/webhooks/twilio/voice` receives call events
- Audio format: mulaw 8 kHz — transcoded to 16 kHz by the backend
- Outbound calls via Twilio's REST API

**Telnyx:**
- Webhook at `/webhooks/telnyx/voice` receives call events
- Audio format: PCM 16 kHz — no transcoding needed
- Outbound calls via Telnyx Call Control API

---

## Call flow — Inbound

```
1. Caller dials the phone number
        │
        ▼
2. Telephony provider (Twilio/Telnyx) receives the call
   and sends a webhook to the backend
        │
        ▼
3. Backend replies with TwiML/TeXML to connect the call
   to the stream WebSocket (/ws/stream/*)
        │
        ▼
4. Provider opens a WebSocket to /ws/stream/* and
   starts sending audio frames
        │
        ▼
5. Backend plays the AI disclosure message
        │
        ▼
6. Backend streams audio to STT provider
   STT returns transcribed text
        │
        ▼
7. Backend sends {type: "message", text: "...", call_id: "..."}
   to the SDK via /ws/sdk
        │
        ▼
8. SDK calls on_message(IncomingMessage) in your code
   Your function returns a string
        │
        ▼
9. SDK sends {type: "response", call_id: "...", text: "..."}
   back to the backend
        │
        ▼
10. Backend sends the text to the TTS provider
    TTS returns audio stream
        │
        ▼
11. Backend streams audio back through the stream WebSocket
    Provider plays it to the caller
        │
        ▼
12. Go to step 6 for the next utterance
        │
        ▼
13. Caller hangs up
    Backend finalizes the CallLog (status=completed, duration)
    Backend sends {type: "call_end"} to the SDK
```

---

## Call flow — Outbound

```
1. Your code calls phone.call(to="+1...", first_message="Hi!")
        │
        ▼
2. SDK sends {type: "call", to: "...", first_message: "..."} via WebSocket
        │
        ▼
3. Backend looks up the account's phone number and provider credentials
        │
        ▼
4. Backend calls the telephony provider's API to initiate the outbound call
   Backend pre-registers the session with the first_message
        │
        ▼
5. Provider calls the destination number
   When the callee answers, provider opens the stream WebSocket
        │
        ▼
6. Backend plays the first_message (via TTS) immediately
        │
        ▼
7. Call continues as inbound from step 6 above
```

---

## Voice mode architecture differences

### Pipeline mode (Deepgram + ElevenLabs)

```
Stream WS → [audio buffer] → Deepgram STT → text → SDK handler
                                                          │
                              ElevenLabs TTS ←── response text
                                    │
                              [audio stream] → Stream WS → Phone
```

### OpenAI Realtime mode

```
Stream WS → [audio buffer] → OpenAI Realtime API WebSocket
                                        │ (text events)
                                   SDK handler
                                        │
                             OpenAI Realtime API (synthesizes)
                                        │ (audio events)
                              Stream WS → Phone
```

The OpenAI Realtime adapter maintains a single bidirectional WebSocket to OpenAI, eliminating the separate STT and TTS round trips.

### ElevenLabs ConvAI mode

```
Stream WS → [audio buffer] → ElevenLabs ConvAI WebSocket
                                        │ (transcript events)
                                   SDK handler
                                        │
                             ElevenLabs ConvAI (synthesizes)
                                        │ (audio events)
                              Stream WS → Phone
```

---

## Data model

### Account

```
accounts
├── id            UUID, primary key
├── email         VARCHAR(255), unique
├── api_key       VARCHAR(64), unique, indexed
└── created_at    TIMESTAMPTZ
```

Each account has many `phone_numbers`.

### PhoneNumber

```
phone_numbers
├── id                    UUID, primary key
├── account_id            UUID, FK → accounts.id
├── number                VARCHAR(20), unique, indexed
├── provider              ENUM('twilio', 'telnyx')
├── provider_sid          VARCHAR(255)
├── provider_credentials  JSON (encrypted)
├── stt_config            JSON (encrypted, nullable)
├── tts_config            JSON (encrypted, nullable)
├── status                ENUM('active', 'suspended')
├── country               VARCHAR(2)
└── created_at            TIMESTAMPTZ
```

`provider_credentials`, `stt_config`, and `tts_config` are encrypted at rest using Fernet symmetric encryption. The encryption key is set via `PATTER_ENCRYPTION_KEY`.

### CallLog

```
call_logs
├── id                UUID, primary key
├── phone_number_id   UUID, FK → phone_numbers.id
├── direction         ENUM('inbound', 'outbound')
├── caller            VARCHAR(20)
├── callee            VARCHAR(20)
├── started_at        TIMESTAMPTZ
├── ended_at          TIMESTAMPTZ (nullable)
├── duration_seconds  INTEGER (nullable)
└── status            ENUM('in_progress', 'completed', 'failed', 'missed')
```

A `CallLog` is created when a call starts (status `in_progress`) and updated when it ends (status `completed` or `failed`, with `ended_at` and `duration_seconds`).

---

## Monorepo structure

```
patter/
├── backend/                  # FastAPI backend
│   ├── app/
│   │   ├── main.py           # App entrypoint, router registration
│   │   ├── config.py         # Pydantic settings (PATTER_ env vars)
│   │   ├── database.py       # SQLAlchemy async session factory
│   │   ├── api/
│   │   │   ├── accounts.py   # Account endpoints
│   │   │   ├── phone_numbers.py
│   │   │   ├── sdk_ws.py     # SDK WebSocket handler
│   │   │   ├── stream_ws.py  # Pipeline stream (Deepgram+ElevenLabs)
│   │   │   ├── stream_realtime.py  # OpenAI Realtime stream
│   │   │   ├── stream_elevenlabs.py # ElevenLabs ConvAI stream
│   │   │   ├── auth.py       # API key helpers
│   │   │   └── stream_auth.py
│   │   ├── models/           # SQLAlchemy ORM models
│   │   ├── providers/
│   │   │   ├── telephony/    # Twilio + Telnyx adapters
│   │   │   ├── stt/          # Deepgram + Whisper adapters
│   │   │   ├── tts/          # ElevenLabs + OpenAI TTS adapters
│   │   │   └── realtime/     # OpenAI Realtime + ConvAI adapters
│   │   ├── services/
│   │   │   ├── call_orchestrator.py
│   │   │   ├── session_manager.py
│   │   │   ├── encryption.py
│   │   │   └── transcoding.py
│   │   └── webhooks/         # Twilio + Telnyx webhook handlers
│   ├── alembic/              # Database migrations
│   └── tests/
├── sdk/                      # Python SDK (pip install patter)
│   └── patter/
│       ├── client.py         # Patter class
│       ├── connection.py     # WebSocket connection
│       ├── models.py         # IncomingMessage, STTConfig, TTSConfig
│       ├── exceptions.py     # PatterError hierarchy
│       └── providers.py      # Provider helper functions
├── sdk-ts/                   # TypeScript SDK (npm install patter)
│   └── src/
│       ├── client.ts         # Patter class
│       ├── connection.ts     # WebSocket connection
│       ├── types.ts          # TypeScript interfaces
│       ├── errors.ts         # Error classes
│       └── providers.ts      # Provider helper functions
└── docs/                     # This documentation
```
