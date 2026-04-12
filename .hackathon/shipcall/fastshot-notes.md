# FastShot Integration Notes — ShipCall

## Overview

ShipCall has two halves: a FastAPI backend (app.py) and a FastShot mobile app. The backend is the brain — it holds the Patter agent, tool handlers, and Twilio integration. The FastShot app is the face — a lightweight mobile UI that displays events and lets the user trigger a call.

## What the FastShot App Needs to Do

The mobile app has exactly two responsibilities:

1. **Display an event feed** — fetch `GET /api/events` and render a list of code events (deploys, errors, test failures).
2. **Trigger a phone call** — send `POST /api/call-me` when the user taps "Call Me Now". The backend handles the rest.

That's it. No auth. No local storage. No settings screen. The phone call is the product.

## API Endpoints

The ShipCall backend exposes these endpoints:

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/health` | Health check. Returns `{"status": "ok", "service": "shipcall"}` |
| `GET` | `/api/events` | Returns mock code events for the event feed |
| `POST` | `/api/call-me` | Triggers an outbound ShipCall to the developer's phone |
| `POST` | `/api/webhook` | Simulates a GitHub webhook (for demo use) |

### GET /api/events — Event Feed

Returns a list of recent code events. The FastShot app fetches this on load and displays the list.

Request: `GET {backend_url}/api/events`

Response:
```json
{
  "events": [
    {
      "id": 1,
      "type": "error_spike",
      "message": "Auth service: 12 failures in 5 min",
      "timestamp": "2026-04-12T11:42:00Z",
      "severity": "critical"
    },
    {
      "id": 2,
      "type": "deploy",
      "message": "v2.3.1 deployed to production",
      "timestamp": "2026-04-12T11:38:00Z",
      "severity": "info"
    },
    {
      "id": 3,
      "type": "test_failure",
      "message": "3 tests failing: test_session_cache",
      "timestamp": "2026-04-12T11:35:00Z",
      "severity": "warning"
    }
  ]
}
```

### POST /api/call-me — Trigger a Call

Triggers an outbound phone call. Optionally accepts a phone number; falls back to `DEV_PHONE_NUMBER` env var.

Request: `POST {backend_url}/api/call-me`
Body (optional): `{"phone_number": "+15551234567"}`

Response:
```json
{
  "status": "calling",
  "message": "ShipCall is calling +15551234567. Pick up your phone!"
}
```

## CORS

CORS is already enabled in app.py with `allow_origins=["*"]`. The FastShot app can call the backend from any origin without issues.

## Backend URL Configuration

The FastShot app needs a base URL pointing to the ShipCall backend. Two options:

1. **ngrok (local development):** Run `ngrok http 8080`, use the generated URL as the base URL in FastShot.
2. **Cloud Run (stable, recommended for submission):** Deploy the backend to Cloud Run using the same `deploy.sh` pattern as VoiceScope. The Cloud Run URL is stable and doesn't expire.

Set this base URL in the FastShot app wherever API calls are configured. All fetch calls go to `{base_url}/api/events` and `{base_url}/api/call-me`.

## FastShot App Screens

### Screen 1: Event Feed
- On load, fetch `GET {base_url}/api/events`
- Display each event as a card or list item: icon/color by severity, message text, timestamp
- Severity colors: `critical` = red, `warning` = yellow/orange, `info` = blue/green
- Big prominent "CALL ME NOW" button at the bottom of the screen

### Screen 2: Call Me Now
- Tapping the button sends `POST {base_url}/api/call-me`
- Show a "Calling you now..." state while the call is being placed
- The developer's phone rings within 5-10 seconds
- No further UI needed — the phone call is the interaction

## Implementation Notes for FastShot

FastShot likely uses `fetch()` or an equivalent HTTP client. Example calls:

```javascript
// Fetch events
const res = await fetch(`${BASE_URL}/api/events`);
const data = await res.json();
// data.events is the array to render

// Trigger a call
const res = await fetch(`${BASE_URL}/api/call-me`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({})  // uses DEV_PHONE_NUMBER from backend env
});
```

All responses are JSON. No authentication required.
