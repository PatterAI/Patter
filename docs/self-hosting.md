# Self-Hosting

Run the full Patter stack on your own infrastructure. No Patter Cloud account is needed — you bring your own telephony provider credentials and voice API keys.

## Prerequisites

- Python 3.11 or higher
- PostgreSQL 14 or higher (or a Supabase project)
- A publicly reachable URL for the backend (telephony webhooks must reach it)
- [ngrok](https://ngrok.com) or similar for local development

**Telephony provider** — at least one of:
- [Twilio](https://twilio.com) account with a phone number
- [Telnyx](https://telnyx.com) account with a phone number and a TeXML app

**Voice providers** — at least one STT and one TTS:
- Deepgram API key (for STT)
- ElevenLabs API key (for TTS)
- OpenAI API key (for Whisper STT, OpenAI TTS, or OpenAI Realtime)

---

## Option A — Without Docker

### 1. Clone the repository

```bash
git clone https://github.com/your-org/patter
cd patter
```

### 2. Create a virtual environment

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
```

### 3. Install backend dependencies

```bash
cd backend
pip install -e ".[dev]"
```

### 4. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` with your values (see [Environment variables reference](#environment-variables-reference) below).

### 5. Set up the database

```bash
alembic upgrade head
```

This creates all required tables. Run this again after pulling new migrations.

### 6. Start the backend

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

The backend is now running at `http://localhost:8000`.

---

## Option B — With Docker Compose

### 1. Clone and configure

```bash
git clone https://github.com/your-org/patter
cd patter
cp .env.example .env
# Edit .env with your values
```

### 2. Start all services

```bash
docker compose up -d
```

This starts:
- `backend` — FastAPI on port 8000
- `postgres` — PostgreSQL on port 5432
- `redis` — Redis on port 6379 (planned for session caching)

### 3. Run migrations

```bash
docker compose exec backend alembic upgrade head
```

### 4. View logs

```bash
docker compose logs -f backend
```

---

## Expose the backend publicly

Telephony webhooks (Twilio, Telnyx) need to reach your backend. For local development, use ngrok:

```bash
ngrok http 8000
```

Copy the HTTPS URL (e.g. `https://abc123.ngrok.io`) and set it as `PATTER_BASE_URL` in your `.env`:

```
PATTER_BASE_URL=abc123.ngrok.io
```

Note: include only the hostname, no scheme or trailing slash.

---

## Configure Twilio

1. Log into the [Twilio Console](https://console.twilio.com).
2. Go to **Phone Numbers** and select your number.
3. Under **Voice Configuration**, set the webhook URL to:
   ```
   https://your-domain.com/webhooks/twilio/voice
   ```
4. Set HTTP method to `POST`.
5. Save.

Your Twilio credentials for the SDK and API:
- `api_key`: Account SID (starts with `AC`)
- `api_secret`: Auth token

---

## Configure Telnyx

1. Log into the [Telnyx Mission Control Portal](https://portal.telnyx.com).
2. Create a **TeXML Application** (under Voice > TeXML Apps).
3. Set the webhook URL to:
   ```
   https://your-domain.com/webhooks/telnyx/voice
   ```
4. Note the **Connection ID** of the TeXML application.
5. Assign your phone number to the TeXML application.

Your Telnyx credentials for the SDK and API:
- `api_key`: Telnyx API key (starts with `KEY4`)
- `connection_id`: The TeXML application connection ID

---

## Create your account

Once the backend is running, create an account to get an API key:

```bash
curl -X POST http://localhost:8000/api/accounts \
  -H "Content-Type: application/json" \
  -d '{"email": "you@example.com"}'
```

Use the returned `api_key` in your SDK:

```python
phone = Patter(
    api_key="pt_xxx",
    backend_url="ws://localhost:8000",
    rest_url="http://localhost:8000",
)
```

---

## Register a phone number

Use the REST API or the SDK's self-hosted mode. The SDK auto-registers when you pass `provider`, `provider_key`, and `number` to `connect()`.

Manual registration:

```bash
curl -X POST http://localhost:8000/api/phone-numbers \
  -H "Content-Type: application/json" \
  -H "X-API-Key: pt_xxx" \
  -d '{
    "number": "+14155550000",
    "provider": "telnyx",
    "provider_credentials": {
      "api_key": "KEY4...",
      "connection_id": "your-connection-id"
    },
    "country": "US",
    "stt_config": { "provider": "deepgram", "api_key": "dg_...", "language": "en" },
    "tts_config": { "provider": "elevenlabs", "api_key": "el_...", "voice": "rachel" }
  }'
```

---

## Environment variables reference

All variables use the `PATTER_` prefix. The backend reads from `.env` in the project root.

| Variable | Required | Default | Description |
|---|---|---|---|
| `PATTER_DATABASE_URL` | Yes | `postgresql+asyncpg://postgres:postgres@localhost:5432/patter` | PostgreSQL connection string. Must use `asyncpg` driver. |
| `PATTER_ENCRYPTION_KEY` | Yes (prod) | `change-me-in-production-32-bytes!` | 32-character key used to encrypt stored provider credentials. **Change in production.** |
| `PATTER_SECRET_KEY` | No | `""` | HMAC signing key for stream auth tokens. |
| `PATTER_BASE_URL` | Yes | `localhost:8000` | Public hostname of the backend (no scheme). Used to construct webhook URLs. |
| `PATTER_ENV` | No | `development` | Set to `production` to enforce required secrets. |
| `PATTER_API_HOST` | No | `0.0.0.0` | Host to bind the server. |
| `PATTER_API_PORT` | No | `8000` | Port to bind the server. |
| `PATTER_REDIS_URL` | No | `redis://localhost:6379/0` | Redis connection string (planned for session caching). |
| `PATTER_WEBHOOK_VALIDATION_ENABLED` | No | `false` | Enable HMAC validation of telephony webhooks. |
| `PATTER_WEBHOOK_SECRET` | No | `""` | Shared secret for webhook validation. |

### Generating a secure encryption key

```bash
python -c "import secrets; print(secrets.token_hex(16))"
```

This prints a 32-character hex string suitable for `PATTER_ENCRYPTION_KEY`.

### Example .env for production

```
PATTER_DATABASE_URL=postgresql+asyncpg://patter:strongpassword@db.example.com:5432/patter
PATTER_ENCRYPTION_KEY=a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6
PATTER_SECRET_KEY=another-long-random-secret
PATTER_BASE_URL=api.yourdomain.com
PATTER_ENV=production
PATTER_WEBHOOK_VALIDATION_ENABLED=true
PATTER_WEBHOOK_SECRET=your-webhook-secret
```

### Example .env for local development

```
PATTER_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/patter
PATTER_ENCRYPTION_KEY=change-me-in-production-32-bytes!
PATTER_BASE_URL=abc123.ngrok.io
PATTER_ENV=development
```

---

## Running migrations

After pulling new code, always run migrations before starting the backend:

```bash
cd backend
alembic upgrade head
```

To create a new migration after changing a model:

```bash
alembic revision --autogenerate -m "describe your change"
alembic upgrade head
```

---

## Using Supabase as the database

Supabase provides managed PostgreSQL with a connection pooler (pgbouncer). Use the **Session mode** connection string to ensure compatibility with SQLAlchemy's async driver:

```
PATTER_DATABASE_URL=postgresql+asyncpg://postgres.[project-ref]:[password]@aws-0-us-east-1.pooler.supabase.com:5432/postgres
```

Avoid transaction mode pooling — it is not compatible with `asyncpg` and SQLAlchemy migrations.

---

## Production checklist

- [ ] `PATTER_ENCRYPTION_KEY` is set to a unique random value
- [ ] `PATTER_ENV=production`
- [ ] `PATTER_BASE_URL` matches your public domain (no `https://`)
- [ ] Database is backed up regularly
- [ ] Backend runs behind a TLS-terminating proxy (nginx, Caddy, etc.)
- [ ] Webhook validation is enabled (`PATTER_WEBHOOK_VALIDATION_ENABLED=true`)
- [ ] Provider credentials are stored only via the API (never in code)
