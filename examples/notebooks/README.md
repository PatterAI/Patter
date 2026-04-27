# Patter Notebook Tutorial Series

24 Jupyter notebooks (12 topics × Python + TypeScript) that walk through every public Patter feature and every supported provider.  Three layers in every notebook let you start with zero credentials and progress to live PSTN calls at your own pace.

## Layer overview

| Layer | Section | Tiers | Requires | Typical runtime |
|-------|---------|-------|----------|----------------|
| Quickstart | §1 | T1 + T2 | Nothing — zero env vars needed | < 30 s |
| Feature Tour | §2 | T1 + T2 + T3 | Per-provider keys; missing keys auto-skip | ~2 min + provider latency |
| Live Appendix | §3 | T4 | `ENABLE_LIVE_CALLS=1` + telephony keys + answering phone | 2–5 min per call |

**Tier definitions**

| Tier | What it does | Cost |
|------|--------------|------|
| T1 — Pure offline | No network. Pure functions, dataclass construction. | Free |
| T2 — Local server | Spins up `EmbeddedServer` in-process, hits it with httpx/fetch. | Free |
| T3 — Provider integration | Real WebSocket to OpenAI / Deepgram / ElevenLabs etc. Sends fixture audio, asserts real response. | Cents per cell |
| T4 — Live PSTN | Real Twilio/Telnyx call placed to `TARGET_PHONE_NUMBER`. Costs real money. | Varies |

---

## Quickstart — Python

```bash
# 1. Copy and fill the env template
cp examples/notebooks/.env.example examples/notebooks/.env

# 2. Install deps
cd examples/notebooks/python
pip install -e ".[dev]"          # or: pip install getpatter python-dotenv ipykernel

# 3. Open a notebook
jupyter lab 01_quickstart.ipynb
```

Hit **Run All** — §1 Quickstart runs end-to-end with no keys in < 30 seconds.

## Quickstart — TypeScript

The TypeScript notebooks use the [Deno Jupyter kernel](https://docs.deno.com/runtime/reference/cli/jupyter/).

```bash
# 1. Install Deno (if not already)
curl -fsSL https://deno.land/install.sh | sh    # macOS/Linux
# or: brew install deno

# 2. Register the Deno Jupyter kernel
deno jupyter --install

# 3. Install npm deps
cd examples/notebooks/typescript
npm install

# 4. Open a notebook
jupyter lab 01_quickstart.ipynb
```

Make sure to select the **Deno** kernel (not Python) when the notebook opens.

---

## Env var — key matrix

Fill `.env` one tier at a time.  Every missing key only skips the cells that need it — it never breaks other cells.

| Env var | Unlocks cells in | Tier |
|---------|-----------------|------|
| `OPENAI_API_KEY` | 01 (realtime), 02 (realtime), 04 (TTS), 05 (LLM), 08 (tools), 09 (guardrails), 10 (advanced), 11 (metrics), 12 (security), all T4 | T3 / T4 |
| `ANTHROPIC_API_KEY` | 05 (LLM — Anthropic provider) | T3 |
| `GOOGLE_API_KEY` | 02 (Gemini Live), 05 (LLM — Gemini provider) | T3 |
| `GROQ_API_KEY` | 05 (LLM — Groq provider) | T3 |
| `CEREBRAS_API_KEY` | 05 (LLM — Cerebras provider) | T3 |
| `DEEPGRAM_API_KEY` | 03 (STT) | T3 |
| `ASSEMBLYAI_API_KEY` | 03 (STT) | T3 |
| `SONIOX_API_KEY` | 03 (STT) | T3 |
| `SPEECHMATICS_API_KEY` | 03 (STT) | T3 |
| `CARTESIA_API_KEY` | 03 (STT), 04 (TTS) | T3 |
| `ELEVENLABS_API_KEY` | 02 (ConvAI), 04 (TTS) | T3 |
| `ELEVENLABS_VOICE_ID` | 04 (TTS — ElevenLabs) | T3 |
| `ELEVENLABS_AGENT_ID` | 02 (ConvAI) | T3 |
| `LMNT_API_KEY` | 04 (TTS) | T3 |
| `RIME_API_KEY` | 04 (TTS) | T3 |
| `ULTRAVOX_API_KEY` | 02 (Ultravox Realtime) | T3 |
| `TWILIO_ACCOUNT_SID` + `TWILIO_AUTH_TOKEN` + `TWILIO_PHONE_NUMBER` | 06 (Twilio §3), all Twilio T4 cells | T4 |
| `TELNYX_API_KEY` + `TELNYX_CONNECTION_ID` + `TELNYX_PHONE_NUMBER` + `TELNYX_PUBLIC_KEY` | 07 (Telnyx §3) | T4 |
| `TARGET_PHONE_NUMBER` | All T4 live-call cells | T4 |
| `NGROK_AUTHTOKEN` | T4 cells without `PUBLIC_WEBHOOK_URL` set | T4 |
| `PUBLIC_WEBHOOK_URL` | T4 cells — overrides ngrok auto-launch | T4 |
| `ENABLE_LIVE_CALLS=1` | All §3 Live Appendix sections (master gate) | T4 |

Budget guards (optional — defaults shown):

```
NOTEBOOK_MAX_CALL_SECONDS=90
NOTEBOOK_MAX_COST_USD=0.25
```

---

## Topic list

| # | Notebook | What you learn |
|---|----------|----------------|
| 01 | `quickstart` | Install, env check, three operating modes (cloud / self-hosted / local), three voice modes (Realtime / ConvAI / Pipeline), minimal agent setup |
| 02 | `realtime` | OpenAI Realtime, Gemini Live, Ultravox, ElevenLabs ConvAI |
| 03 | `pipeline_stt` | Deepgram, Whisper, AssemblyAI, Soniox, Speechmatics, Cartesia STT |
| 04 | `pipeline_tts` | ElevenLabs, OpenAI, Cartesia, LMNT, Rime TTS |
| 05 | `pipeline_llm` | OpenAI, Anthropic, Gemini, Groq, Cerebras; custom `on_message`; `LLMLoop`; tool-call protocol |
| 06 | `telephony_twilio` | Webhook parsing, HMAC-SHA1, AMD, DTMF, recording, transfer, status callback, TwiML |
| 07 | `telephony_telnyx` | Call Control flows, Ed25519 signatures, AMD, DTMF, anti-replay |
| 08 | `tools` | `@tool` / `tool()` factory, auto-injected `transfer_call`/`end_call`, dynamic variables, JSON schema validation |
| 09 | `guardrails_hooks` | Keyword block, PII redact, pipeline hooks, text transforms, sentence chunker |
| 10 | `advanced` | Scheduler (cron / once / interval), fallback LLM chain, background audio mixer, noise filter, custom providers |
| 11 | `metrics_dashboard` | `CallMetricsAccumulator`, `MetricsStore`, dashboard SSE, CSV/JSON export, pricing, basic auth |
| 12 | `security` | Twilio HMAC, Telnyx Ed25519, SSRF guard, webhook URL validation, secret hygiene, cost cap |

---

## Running all notebooks headlessly

```bash
# Python
cd examples/notebooks
bash scripts/run_all_notebooks.sh python

# TypeScript
bash scripts/run_all_notebooks.sh typescript
```

The script executes every notebook with `jupyter nbconvert --execute`, captures outputs, and strips them before writing back (keeping the repo output-free).  Exit code is non-zero if any notebook fails.

---

## Troubleshooting

### Missing API keys — cells just skip

A missing key never breaks a notebook.  You'll see a yellow skip banner:

```
⚪️ SKIPPED  deepgram_stt  (tier=3, missing: DEEPGRAM_API_KEY)
```

Add the key to `.env`, restart the kernel, and re-run just that cell.

### Deno kernel not found

```
No kernel matching 'deno' found
```

Install the kernel once:

```bash
deno jupyter --install
```

Then refresh the kernel list in JupyterLab (`Kernel → Change Kernel…`).

### ngrok not launching (T4 cells)

T4 cells require a public webhook URL so Twilio/Telnyx can reach your machine.  Two options:

**Option A — set `PUBLIC_WEBHOOK_URL` manually**

Start ngrok in a separate terminal:

```bash
ngrok http 8765
```

Copy the `https://xxxx.ngrok-free.app` URL into `.env`:

```
PUBLIC_WEBHOOK_URL=https://xxxx.ngrok-free.app
```

**Option B — let the notebook auto-launch ngrok**

Set `NGROK_AUTHTOKEN` in `.env`.  The cell calls `_setup.auto_ngrok()` which starts ngrok in-process and writes `PUBLIC_WEBHOOK_URL` automatically.

### Live call does not connect (T4)

Check in order:

1. `ENABLE_LIVE_CALLS=1` is set in `.env` (master gate).
2. `TARGET_PHONE_NUMBER` is a reachable E.164 number you can answer.
3. `TWILIO_PHONE_NUMBER` / `TELNYX_PHONE_NUMBER` is provisioned and voice-capable in your carrier account.
4. `PUBLIC_WEBHOOK_URL` is reachable from the public internet (test with `curl $PUBLIC_WEBHOOK_URL/health`).
5. Ring timeout default is 90 s — if the call connects but the agent never speaks, check your `OPENAI_API_KEY`.

### Python version

Requires **Python 3.11+**.  Python 3.13 automatically installs `audioop-lts` (declared as a conditional dep in `pyproject.toml`).

### TypeScript / Node version

Requires **Node 18+**.  The Deno kernel ships its own runtime; the `npm install` step only installs `getpatter` for static resolution.

---

## Outputs are stripped from committed notebooks

Notebooks are committed **without outputs** (enforced by `nbstripout`).  This keeps diffs small and avoids committing provider-specific latencies or partial transcripts.

Run `nbstripout --install` in the repo root to register the pre-commit hook locally.

---

See `RELEASES.md` for the per-release manual run log.
