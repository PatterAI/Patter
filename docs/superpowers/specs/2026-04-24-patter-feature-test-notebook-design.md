# Patter Feature-Test Notebook Series — Design Spec

**Date:** 2026-04-24
**Status:** Approved by Francesco — handing off to writing-plans.
**Working directory:** `[patterai]-Patter` (the published `getpatter` SDK monorepo).

## 1. Goal & non-goals

### Goal

Ship a **24-notebook customer-facing tutorial series** (Python + TypeScript, parallel pairs) that lets a reader learn and verify **every public Patter feature and every supported provider**, with a clear ramp from "no keys at all" to "real PSTN call" inside every notebook.

### Reader journey

1. Clone the repo. Open `examples/notebooks/python/01_quickstart.ipynb`.
2. Hit "Run All". The Quickstart layer (T1 + T2) executes end-to-end with **zero API keys** in <30 seconds.
3. Add provider keys to `.env` as the reader wants to explore. The Feature Tour layer (T1 + T2 + T3) runs each provider individually; missing keys auto-skip with a friendly markdown banner naming the env var.
4. Set `ENABLE_LIVE_CALLS=1`, plug in carrier credentials, and run the Live Appendix to place real Twilio/Telnyx calls. Off by default in every notebook.

### Execution tiers (used throughout)

| Tier | What it does | Cost / Risk | Key gating |
|------|--------------|-------------|------------|
| **T1 — Pure offline** | No network. Pure functions, dataclass construction. | Free, instant. | None. |
| **T2 — Local server only** | Spins up `EmbeddedServer` in-process, hits its endpoints with `httpx`/`fetch`. No carrier, no provider keys. | Free, ~1s startup. | None. |
| **T3 — Provider integration (no phone)** | Real WS to OpenAI Realtime / Deepgram / ElevenLabs / etc. Sends mock PCM audio bytes from a fixture, asserts a real transcript/audio frame comes back. | Few cents per cell, real key needed. | Per-key skip via `_setup.cell`. |
| **T4 — Live PSTN call** | Real Twilio/Telnyx call placed or received. Needs phone, ngrok, a number that answers. | Real $$, manual setup. | `ENABLE_LIVE_CALLS=1` plus per-key skip. |

### Layer mapping (per notebook)

- **§1 Quickstart layer** = T1 + T2 only — anyone can run end-to-end with zero keys.
- **§2 Feature Tour layer** = T1 + T2 + T3 — every feature/provider lights up; full series costs a few dollars.
- **§3 Live Appendix** = T4 — gated behind `ENABLE_LIVE_CALLS=1`.

### Non-goals

- **Not a regression suite.** The acceptance repo (`[nicolotognoni]-patter-sdk-acceptance`) keeps that role with pytest + vitest. The notebooks may share fixtures with it, but they are read top-to-bottom, not invoked by CI as test cases.
- **Not a benchmark.** Latency/cost numbers live in `scripts/latency_benchmark.py` already.
- **Not a replacement for `examples/`.** The standalone `.py`/`.ts` examples remain authoritative one-file demos. The notebooks are the *narrated, exhaustive* layer.
- **No new SDK features.** If a feature is missing or broken, file it in the acceptance repo's `BUGS.md`; do not paper over in the notebook.

## 2. File layout & topic list

### On-disk layout

```
examples/notebooks/
├── README.md                        # series overview, quickstart, key matrix, troubleshooting
├── RELEASES.md                      # per-version observation log (filled during phase 5)
├── .env.example                     # every env var any notebook reads, grouped by tier
├── fixtures/
│   ├── audio/
│   │   ├── hello_world_8khz_mulaw.wav      # ~3s "hello world" for STT cells
│   │   ├── hello_world_16khz_pcm.wav
│   │   ├── voicemail_beep.wav              # AMD detection
│   │   ├── background_music_loop.wav       # background audio mixer
│   │   └── PROVENANCE.md                   # source/license documentation
│   ├── webhooks/
│   │   ├── twilio_voice_inbound.json
│   │   ├── twilio_status_callback.json
│   │   ├── telnyx_call_initiated.json
│   │   └── telnyx_dtmf_received.json
│   └── keys/
│       ├── telnyx_test_ed25519_pub.pem
│       └── telnyx_test_ed25519_priv.pem
├── python/
│   ├── _setup.py                    # shared: load .env, key matrix, skip helper, ngrok launcher
│   ├── 01_quickstart.ipynb
│   ├── 02_realtime.ipynb
│   ├── 03_pipeline_stt.ipynb
│   ├── 04_pipeline_tts.ipynb
│   ├── 05_pipeline_llm.ipynb
│   ├── 06_telephony_twilio.ipynb
│   ├── 07_telephony_telnyx.ipynb
│   ├── 08_tools.ipynb
│   ├── 09_guardrails_hooks.ipynb
│   ├── 10_advanced.ipynb
│   ├── 11_metrics_dashboard.ipynb
│   └── 12_security.ipynb
└── typescript/
    ├── _setup.ts
    ├── tsconfig.json                # Deno-friendly config for the kernel
    ├── 01_quickstart.ipynb
    └── ...                          # 12 mirrored files
```

### Topic list

| # | Notebook | Covers |
|---|---|---|
| 01 | `quickstart` | install, env check, three operating modes (cloud / self-hosted / local), three voice modes (Realtime / ConvAI / Pipeline), "hello phone" minimal agent |
| 02 | `realtime` | OpenAI Realtime, Gemini Live, Ultravox, ElevenLabs ConvAI |
| 03 | `pipeline_stt` | Deepgram, Whisper, AssemblyAI, Soniox, Speechmatics, Cartesia |
| 04 | `pipeline_tts` | ElevenLabs, OpenAI, Cartesia, LMNT, Rime |
| 05 | `pipeline_llm` | OpenAI, Anthropic, Gemini, Groq, Cerebras, custom `on_message`, `LLMLoop`, tool-call protocol |
| 06 | `telephony_twilio` | webhook parsing, HMAC-SHA1, AMD, DTMF, recording, transfer, ring timeout, status callback, TwiML emission |
| 07 | `telephony_telnyx` | Call Control flows, Ed25519 sigs, AMD, DTMF, track filter, anti-replay |
| 08 | `tools` | `@tool`/`defineTool`, auto-injected `transfer_call`/`end_call`, dynamic variables, custom tools, JSON schema validation |
| 09 | `guardrails_hooks` | keyword block, PII redact, pipeline hooks (`before_send_to_stt` etc.), text transforms (markdown/emoji filters), sentence chunker |
| 10 | `advanced` | scheduler (cron/once/interval), fallback LLM chain, background audio mixer, noise filter, custom STT/TTS, custom LLM HTTP endpoint |
| 11 | `metrics_dashboard` | `CallMetricsAccumulator`, `MetricsStore`, dashboard SSE, CSV/JSON export, pricing, basic auth |
| 12 | `security` | Twilio HMAC, Telnyx Ed25519, SSRF guard, webhook URL validation, secret hygiene, dashboard auth, cost cap |

## 3. Per-notebook layered template

Every notebook follows the same skeleton — same shape across all 24 files makes the series easy to skim and easy to maintain.

```
┌─ Markdown: Title + 1-paragraph "what you'll learn"
├─ Markdown: Prerequisites table
│       (which tier is needed, which env vars unlock which cells)
│
├─ Cell: %load_ext autoreload + import _setup
│       env = _setup.load()
│       _setup.print_key_matrix(env, required=["OPENAI_API_KEY", ...])
│       # prints a ✅/⚪️ table per cell so the reader sees what will run/skip
│
├─ ── Section §1: Quickstart (T1 + T2 only) ──
│   Runs with zero keys. ~5 cells. Produces visible output every cell.
│   - Imports + version check
│   - Construct a Patter instance in local mode (no keys)
│   - Validate an E.164 number, render TwiML, parse a canned webhook
│   - Spin up EmbeddedServer, hit /health with httpx, shut it down
│   - Show what's coming next in the notebook
│
├─ ── Section §2: Feature Tour (T1 + T2 + T3) ──
│   The exhaustive layer. One feature/provider per cell.
│   Each T3 cell is wrapped:
│       with _setup.cell("deepgram_stt", tier=3,
│                        required=["DEEPGRAM_API_KEY"]):
│           ...real WS to Deepgram, send fixture audio, assert transcript
│   Skips render as a clear yellow banner output, NOT an exception.
│   Cells are ordered: simplest → most complex within the topic.
│
└─ ── Section §3: Live Appendix (T4) ──
    Gated at the top:
        if not env.enable_live_calls:
            _setup.skip_section("Set ENABLE_LIVE_CALLS=1 to enable real calls.
                                 Costs apply. Have a phone you can answer.")
    Cells inside place/receive real PSTN calls.
    Hard caps: 90s call duration, $0.25 cost cap (env-overridable).
    ngrok auto-launches if PUBLIC_WEBHOOK_URL is unset.
    Each call is wrapped with a finally: hangup-leftover-calls sweep.
```

### Cell template

```python
# ── Feature: Deepgram STT ────────────────────────────────────────
# Tier: T3 (real provider WS, ~$0.001 per cell)
# Requires: DEEPGRAM_API_KEY

with _setup.cell("deepgram_stt", tier=3, required=["DEEPGRAM_API_KEY"]):
    from patter.providers import DeepgramSTT
    stt = DeepgramSTT(api_key=env.deepgram_key, language="en-US")
    audio = _setup.load_fixture("audio/hello_world_8khz_mulaw.wav")
    transcript = await _setup.run_stt(stt, audio)
    print(f"transcript: {transcript!r}")
    assert "hello" in transcript.lower()
```

The `_setup.cell(...)` context manager handles: tier-gating (skip if cell tier > current run tier), key gating, timing, exception → friendly banner, cost accumulator (live appendix only). One pattern, every cell. The TS `_setup.ts` mirrors it.

## 4. Cross-cutting infrastructure

### `_setup.py` / `_setup.ts` — shared helpers

```python
# _setup.py — public surface (TS mirror is identical in shape, camelCase)

@dataclass(frozen=True)
class NotebookEnv:
    openai_key: str
    elevenlabs_key: str
    deepgram_key: str
    # ... all 18 keys we already enumerated, mirroring AcceptanceEnv
    enable_live_calls: bool
    max_call_seconds: int = 90
    max_cost_usd: float = 0.25

def load() -> NotebookEnv: ...
def has_key(env: NotebookEnv, name: str) -> bool: ...
def print_key_matrix(env, required: list[str]) -> None: ...
def load_fixture(path: str) -> bytes: ...           # resolves under fixtures/
def cell(name: str, *, tier: int, required: list[str] = ()) -> ContextManager: ...
def skip(reason: str) -> None: ...
def skip_section(reason: str) -> None: ...
def run_stt(stt, audio: bytes) -> str: ...          # standardised STT roundtrip
def run_tts(tts, text: str) -> bytes: ...
def hangup_leftover_calls(env) -> None: ...         # Twilio + Telnyx sweep
```

Roughly 250 lines per language. Keeps notebook cells short and intent-revealing.

### Fixtures

- **Audio:** 4 short WAV/PCM clips, ≤200 KB each. Generated via gTTS or Piper at scaffolding time. `PROVENANCE.md` documents licensing.
- **Webhook bodies:** redacted captures from real Twilio/Telnyx events. Phone numbers replaced with `+15555550100` per the security rule.
- **Test keypair:** committed Ed25519 keypair *only* for the signature-cell roundtrip. Filename matches the security-rule whitelist (`*_test_*`).

### Graceful skip pattern

- Missing key → yellow banner output, cell exits cleanly.
- Wrong tier (e.g. `ENABLE_LIVE_CALLS=0` and a T4 cell) → cell exits cleanly with explanatory banner.
- Real exception inside a feature → banner + truncated traceback, but the notebook keeps running.

### Parity discipline

- Every Python notebook has a TS twin with the **same section structure, same cell ordering, same cell names**.
- `scripts/check_notebook_parity.py` diffs the structure of each pair and fails CI if a cell name or order drifts. ~80 lines.
- Per-PR check; daily `docs-feature-drift` cron continues to cover the SDK ↔ docs side.

### Reproducibility

- Notebooks committed **without outputs** (`nbstripout` pre-commit hook). Reasoning: outputs include real provider responses (cost, latencies, transcripts) that drift call-to-call.
- `.env.example` pins `PATTER_VERSION=0.5.2` with a comment that the notebook works against any release ≥ that. `_setup.load()` prints the installed `getpatter` version on every notebook open so drift is visible.

## 5. Per-topic feature inventory

Each row below is one cell in §2 Feature Tour of that notebook. Quickstart and Live Appendix cells are *additional*. Cells appear in execution order.

### 01 quickstart.ipynb (§2: 6 cells)
1. `version_check` — print `getpatter.__version__`, Python/Node version, dotenv check
2. `e164_validation` — `validate_e164` happy path + invalid input
3. `local_mode_construction` — `Patter(twilio_sid=..., phone_number=...)`, `mode == "local"`
4. `cloud_mode_construction` — `Patter(api_key="pt_...")`, `mode == "cloud"`
5. `agent_factory_three_voice_modes` — build one agent per voice mode, assert handler classes
6. `embedded_server_lifecycle` — start/stop, hit `/health`, hit `/api/v1/calls` empty

### 02 realtime.ipynb (§2: 4 cells)
1. `openai_realtime` — open WS, send 1s of fixture audio, assert audio response delta
2. `gemini_live` — same shape, Google
3. `ultravox_realtime` — same shape, Ultravox
4. `elevenlabs_convai` — open ConvAI WS, assert agent greeting

### 03 pipeline_stt.ipynb (§2: 6 cells)
`deepgram`, `whisper`, `assemblyai`, `soniox`, `speechmatics`, `cartesia` — one cell each, identical roundtrip via `_setup.run_stt`, asserts non-empty transcript containing target word.

### 04 pipeline_tts.ipynb (§2: 5 cells)
`elevenlabs`, `openai`, `cartesia`, `lmnt`, `rime` — one cell each, identical roundtrip via `_setup.run_tts`, asserts ≥1 audio chunk and total bytes > threshold. Each cell ends with an `IPython.display.Audio(...)` so the reader can hear it.

### 05 pipeline_llm.ipynb (§2: 8 cells)
1–5. `openai`, `anthropic`, `gemini`, `groq`, `cerebras` — simple chat completion via `LLMLoop` with `OpenAILLMProvider` swapped per provider.
6. `custom_on_message` — pass `on_message=` callback, assert it intercepts.
7. `llm_loop_tool_call` — register `@tool`, send "what time is it", assert the tool fires and result feeds back.
8. `llm_loop_streaming_tokens` — assert async iterator yields >1 chunk.

### 06 telephony_twilio.ipynb (§2: 9 cells)
1. `parse_inbound_voice_webhook`
2. `verify_signature_valid`
3. `verify_signature_invalid`
4. `amd_voicemail_branch`
5. `dtmf_input`
6. `recording_url_received`
7. `transfer_call_twiml`
8. `ring_timeout_emission`
9. `status_callback_lifecycle`

### 07 telephony_telnyx.ipynb (§2: 7 cells)
`call_initiated_event_parsing`, `verify_ed25519_valid`, `verify_ed25519_invalid_replay`, `track_filter_inbound_only`, `dtmf_received_event`, `transfer_call_call_control`, `ring_timeout_emission` (Telnyx `timeout_secs`).

### 08 tools.ipynb (§2: 7 cells)
1. `tool_decorator_basic` — `@tool` on a sync function, schema generated correctly
2. `tool_decorator_async`
3. `auto_injected_transfer_call` — appears in agent's tool list without declaration
4. `auto_injected_end_call`
5. `dynamic_variables` — render `system_prompt` with `{customer_name}` placeholder
6. `tool_argument_validation` — bad args trigger schema rejection, not handler call
7. `tool_returns_streamed_to_llm` — tool result string round-trips into next LLM turn

### 09 guardrails_hooks.ipynb (§2: 6 cells)
1. `keyword_block_guardrail` — block "secret_word", assert response replaced
2. `pii_redact_guardrail` — strips phone/email from outbound TTS text
3. `before_send_to_stt_hook`
4. `before_send_to_llm_hook`
5. `before_send_to_tts_hook`
6. `text_transforms` — `filterMarkdown`, `filterEmoji`, `filterForTTS` + `SentenceChunker.chunk` boundary cases

### 10 advanced.ipynb (§2: 7 cells)
1. `scheduler_cron`
2. `scheduler_once`
3. `scheduler_interval`
4. `fallback_llm_chain` — primary raises, secondary succeeds, observed in metrics
5. `background_audio_mixer`
6. `noise_filter` — Krisp/DeepFilterNet wrapper, asserts output ≠ input
7. `custom_stt_via_protocol` — implement minimal `STTProvider` Protocol, plug into pipeline

### 11 metrics_dashboard.ipynb (§2: 7 cells)
1. `call_metrics_accumulator_basic`
2. `pricing_overrides`
3. `metrics_store_eviction`
4. `metrics_store_csv_export`
5. `metrics_store_json_export`
6. `dashboard_sse_subscribe`
7. `dashboard_basic_auth`

### 12 security.ipynb (§2: 8 cells)
1. `twilio_hmac_roundtrip`
2. `twilio_hmac_tamper`
3. `telnyx_ed25519_roundtrip`
4. `telnyx_replay_window`
5. `ssrf_guard_private_ip`
6. `ssrf_guard_metadata_endpoint`
7. `dashboard_basic_auth_default_off_when_public`
8. `secret_log_redaction`

**Totals:** 80 Feature-Tour cells across 12 topic notebooks, plus ~40 Quickstart cells and ~25 Live-Appendix cells. ×2 for TypeScript ≈ **290 distinct runnable cells.**

**Live Appendix sketches** (full enumeration deferred to writing-plans): every topic gets 1–3 live cells. Examples — 01: 5-second outbound to your number; 02: real Realtime call on Twilio; 06: receive an inbound call; 07: same for Telnyx; 08: fire `transfer_call` mid-call.

## 6. Acceptance criteria, phasing, risks, maintenance

### Acceptance criteria

The work is "done" when:
1. All 24 `.ipynb` files exist with §1/§2/§3 sections; `_setup.{py,ts}` and the fixtures tree are present and committed without secrets.
2. Headless run with **no env vars** set: every Quickstart cell passes; every Feature Tour cell renders a skip banner; Live Appendix exits at the gate. Zero exceptions surface.
3. Headless run with **all keys + `ENABLE_LIVE_CALLS=0`**: every Feature Tour cell either passes or renders a friendly banner; total wall-clock under 8 min, total spend under $2.
4. Manual run with **all keys + `ENABLE_LIVE_CALLS=1`**: at least one live cell per topic completes a real call within budget caps.
5. `scripts/check_notebook_parity.py` is green — Python ↔ TS structure matches.
6. CI: a `notebooks-quickstart` job runs the Quickstart layer of every notebook headless on every PR (no keys needed) and asserts cell outputs are empty after `nbstripout`.
7. README explains how to run, the per-cell key matrix, and troubleshooting (missing keys, ngrok setup, Deno kernel install).

### Phasing — five PRs

| Phase | Scope | Acceptance |
|---|---|---|
| 1 — Skeleton | Tree, README, `_setup.{py,ts}` full surface, fixtures, parity check, CI quickstart job, empty scaffolds for all 24 notebooks | Scaffolds open in Jupyter without import errors and parity-check is green |
| 2 — Quickstart everywhere | Fill §1 in all 24 notebooks (T1 + T2 only) | Headless no-keys run is fully green |
| 3 — Feature tour | Fill §2 cells, one PR per topic 01→12 | Each topic's PR exercises its real provider keys, all cells pass or skip cleanly |
| 4 — Live appendix | Fill §3 cells, can run in parallel after phase 3 | Manual live-call session completes one full pass per topic; budget caps respected |
| 5 — Polish | README, RELEASES.md, hook into `docs-feature-drift` cron, launcher script | Drift cron flags missing notebook cells when the feature inventory grows |

Cell count target: 80 Feature-Tour + ~40 Quickstart + ~25 Live-Appendix ≈ 145 cells per language; ×2 ≈ 290 runnable cells across the full series.

### Risks and mitigations

1. **Deno kernel ergonomics for TS `.ipynb`** — top-level await, file-relative imports, async iterator syntax inside cells all have rough edges. *Mitigation:* prototype `01_quickstart.ts.ipynb` end-to-end in phase 1 before scaffolding the other 11; if Deno is blocking, fall back to single-language Python plus pinned TS snippets — flagged as a course-correction at the phase-1 review, not silently.
2. **Provider rate limits during a full T3 run** — Groq and Cerebras free tiers ratelimit aggressively. *Mitigation:* `_setup.cell` accepts an optional `min_interval_s=` per-provider; the run-all helper serialises same-provider cells.
3. **Audio fixture licensing** — generated audio must be free-to-distribute. *Mitigation:* generate the 4 audio clips with `gTTS` or Piper at scaffolding time; commit `fixtures/audio/PROVENANCE.md`.
4. **PII in committed webhook fixtures** — redaction has historically missed Twilio SIDs. *Mitigation:* `_setup.load_fixture` calls a `_assert_redacted()` self-check on every load; pre-commit grep over `examples/notebooks/fixtures/` flags any pattern outside the security-rule whitelist.
5. **Keys leaking via cell outputs** — without strict output stripping, transcripts and signed URLs can land in commits. *Mitigation:* `nbstripout` enforced as a pre-commit hook, plus a CI job that greps for `sk-`, `AC`, `KEY`, `xox` patterns inside `examples/notebooks/**/*.ipynb`.
6. **SDK API drift** — between writing and shipping a notebook, public symbols may rename. *Mitigation:* pin `PATTER_VERSION` in `.env.example`; parity script warns when an `import` line resolves to a deprecated export.

### Maintenance story

- Every shipped feature must (a) add an `xlsx` inventory row per the existing `documentation-best-practices.md` rule, **and** (b) add a Feature-Tour cell to the appropriate notebook. The `docs-sync` subagent's responsibilities expand: on inventory growth, dispatch a follow-up to insert the missing notebook cell (Python and TS).
- The daily `docs-feature-drift` cron grows a `feature-vs-notebook-drift` check: a new feature without a notebook cell in either language opens an issue labelled `notebook-drift`.
- On every `getpatter` minor bump: re-run `notebooks-quickstart` CI; on every minor *release*, manually `Run All` once per topic with full keys and append a row to `examples/notebooks/RELEASES.md` (date, version, observations, any cell that newly skipped or broke).

## Decisions log (for traceability)

- **Audience:** customer-facing tutorial in main SDK repo (chosen over internal acceptance / scratch).
- **Granularity:** every single feature AND every single provider gets a runnable cell.
- **Tier mapping:** Quickstart=T1+T2, Feature Tour=T1+T2+T3, Live Appendix=T4 gated.
- **Languages:** parallel Python and TypeScript notebooks (Deno kernel for TS), 24 files total.
- **Topic split:** 12 topics — one notebook each for quickstart, realtime, STT, TTS, LLM, Twilio, Telnyx, tools, guardrails+hooks, advanced, metrics+dashboard, security.
- **Layout:** split by language under `examples/notebooks/{python,typescript}/`; shared `fixtures/` and `README.md`.
