# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is Patter

Patter is an open-source voice AI platform that connects AI agents to phone calls. It has two SDKs (Python + TypeScript) with full feature parity, an optional cloud backend, and a local/embedded mode.

## Key Rules

1. **Dual-SDK parity is non-negotiable.** Every user-visible feature must exist in both `sdk/` (Python) and `sdk-ts/` (TypeScript) with matching names, defaults, and semantics. A PR that adds or changes one side without the other is incomplete.
2. **Agent team for non-trivial work.** For any multi-file change, cross-package work, or feature implementation, create an agent team via `TeamCreate` before coding. Single-file fixes and questions do not need a team.
3. **No AI attribution in commits.** No "Generated with Claude" footers, no "Co-Authored-By: Claude" lines, no AI references in commit messages or PR descriptions.
4. **Keep the changelog current.** Every non-trivial change ends with an appended entry in the root `CHANGELOG.md`.
5. **Every generated doc filename starts with `YYYY-MM-DD-`.** Any new Markdown artifact created as part of a task — ExecPlans, ADRs, status snapshots, RFCs, migration notes, research findings, retrospectives, meeting notes — must use a filename that begins with the creation date in ISO 8601 form, followed by a category tag and a slug. Example: `2026-04-11-PLAN-telnyx-e164-validation.md`. The only exceptions are conventional repo files with fixed names (`README.md`, `CHANGELOG.md`, `CLAUDE.md`, `CONTRIBUTING.md`, `LICENSE`, `RELEASE_PROCESS.md`, Mintlify navigation files). This keeps the docs directories file-name-sortable by age and makes staleness obvious at a glance.
6. **Read prior docs before non-trivial work.** Before starting any multi-file change or feature, scan recent project history using the procedure below. Extend existing ExecPlans rather than starting parallel ones.

   **Session context scan (mandatory before non-trivial implementation):**

   ```bash
   # List the 5 most recent dated docs across all internal doc directories (newest first).
   # The YYYY-MM-DD- prefix means reverse-sort = newest-first.
   { ls -1 docs/plans/ docs/adr/ docs/status/ 2>/dev/null; } | sort -r | head -5
   ```

   Spawn a Haiku agent (`model: "haiku"`) to read each of the 5 files plus the top of `CHANGELOG.md`. The agent must return a **2-3 line summary per file**: what changed, what was decided, and what is still open. No full reproductions — just enough to orient the current session.

   If the summaries reveal that more context is needed (e.g., the recent docs reference an earlier plan or ADR), expand to the 10 most recent files and re-scan. Do not go beyond 10 — if the history is that deep, read the specific file referenced by name instead of scanning blindly.

   This scan replaces guessing. A new session has zero memory of prior work; these summaries are how it catches up.

## Repository Structure

```
sdk/          Python SDK (pip install patter)
sdk-ts/       TypeScript SDK (npm install patter)
backend/      FastAPI cloud backend (PostgreSQL, not needed for local mode)
website/      Marketing site (static HTML)
docs/         Mintlify documentation site (user-facing SDK reference)
examples/     30 examples organized by developer/ and enterprise/
```

## Build & Test Commands

### Python SDK

```bash
cd sdk
pip install -e ".[local,dev]"          # Install with all extras
pytest tests/ -v                        # Run all tests
pytest tests/test_metrics.py -v         # Run single test file
pytest tests/ -v --tb=short             # Short traceback (CI style)
pytest --cov=patter --cov-report=term-missing  # Coverage
```

- Python 3.11+ required
- pytest-asyncio with `asyncio_mode = "auto"` (no need for `@pytest.mark.asyncio`)
- Frozen dataclasses for all models (`sdk/patter/models.py`)

### TypeScript SDK

```bash
cd sdk-ts
npm install                             # Install deps
npm test                                # Run tests (vitest)
npm run test:watch                      # Watch mode
npm run lint                            # Type check (tsc --noEmit)
npm run build                           # Build dist (tsup: CJS + ESM + .d.ts)
```

- Node 18+ required
- vitest for testing
- tsup for bundling (CJS + ESM + declaration files)
- Strict TypeScript (`tsconfig.json` strict: true)

## Architecture

### Three Operating Modes

1. **Cloud** — SDK connects via WebSocket to `wss://api.patter.dev`. SDK only exchanges text, backend handles all audio.
2. **Self-Hosted** — Same as cloud but pointing to your own backend instance.
3. **Local** — SDK spawns an embedded server (FastAPI in Python, Express in TS) that handles telephony webhooks, audio streaming, STT/TTS directly. No backend needed.

### Voice Provider Modes

- **OpenAI Realtime** — All-in-one: STT + LLM + TTS in a single WebSocket. Lowest latency.
- **ElevenLabs ConvAI** — ElevenLabs-managed conversational AI endpoint.
- **Pipeline** — Modular: separate STT → LLM → TTS. Any provider combination. Highest flexibility.

### Call Flow (Local Mode)

```
Phone → Twilio/Telnyx webhook → EmbeddedServer → StreamHandler
  StreamHandler creates provider adapter (Realtime/ElevenLabs/Pipeline)
  Pipeline: Audio → STT provider → LLMLoop (tool calling) → TTS provider → Audio back
  Realtime: Audio → OpenAI Realtime API (handles everything) → Audio back
```

### Key Abstractions

- **`Patter`** (`client.py` / `client.ts`) — Main entry point. Creates agents, manages connections.
- **`Agent`** (`models.py` / `types.ts`) — Immutable config: system prompt, voice, model, tools, guardrails.
- **`StreamHandler`** (`handlers/stream_handler.py` / `stream-handler.ts`) — Per-call session manager. Routes audio between telephony and voice providers.
- **`LLMLoop`** (`services/llm_loop.py` / `llm-loop.ts`) — Pipeline-mode LLM orchestration with streaming + tool calling (3x retry, 10s timeout, 10 max iterations).
- **`CallMetricsAccumulator`** (`services/metrics.py` / `metrics.ts`) — Per-turn latency + cost tracking.
- **`MetricsStore`** (`dashboard/store.py` / `dashboard/store.ts`) — In-memory circular buffer (500 calls) powering the dashboard.

### Telephony Adapters

- **Twilio**: mulaw 8kHz audio, mark-based barge-in tracking, TwiML for call control
- **Telnyx**: PCM 16kHz audio, Call Control API, Ed25519 webhook verification

### Built-in Tools (auto-injected)

- `transfer_call` — Transfers to a phone number (E.164 validated)
- `end_call` — Terminates the call with optional reason

## Code Conventions

- **Feature parity**: every feature must exist in both Python and TypeScript SDKs.
- **Immutable config**: Python uses `@dataclass(frozen=True)`, TypeScript uses `readonly` interface fields.
- **All features opt-in**: new config fields must be optional with sensible defaults.
- **Logging**: Python uses `logging.getLogger("patter")`, never `print()`. TS uses `console.log/warn/error`.
- **Async everywhere**: Python is fully async/await. TS uses async for all I/O.
- **Provider-agnostic**: new features should work across all voice modes where applicable.

## Environment & Secrets

Patter reads configuration from environment variables. The list below describes typical env vars for a Patter deployment; verify against `sdk/patter/config.py` and `sdk-ts/src/config.ts` before relying on it.

- **LLM providers**: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` (if used). Optional global override via `PATTER_DEFAULT_MODEL`.
- **Voice providers**: `ELEVENLABS_API_KEY`, `DEEPGRAM_API_KEY`, `CARTESIA_API_KEY`. For each pipeline provider, see its adapter module for the exact variable name it expects — adapters should not invent new env vars without registering them in the config module.
- **Telephony**: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TELNYX_API_KEY`, and `TELNYX_PUBLIC_KEY` for Ed25519 webhook signature verification.
- **Cloud/backend**: `PATTER_CLOUD_URL` (defaults to `wss://api.patter.dev`), `DATABASE_URL` (backend only, cloud/self-hosted mode).
- **Local dev**: `PATTER_LOG_LEVEL`, `NGROK_AUTHTOKEN` (optional, for exposing a local server to telephony webhooks).

Rules for handling secrets:

- Load via `.env` — python-dotenv in the Python SDK, dotenv in the TypeScript SDK. Never read secrets from hard-coded literals, even "just for testing".
- Never commit `.env` files. Commit `.env.example` only, with placeholder values and a one-line comment per variable explaining what it is and where to obtain it.
- Never log secret values, even at DEBUG. If you need to verify a key is loaded, log its presence or its length — never its content.
- Rotate any key that lands in a repo, a log file, or a CI artifact. Treat exposure as permanent: rotation is the only remediation.
- Fail fast at startup with a clear, human-readable message if a required secret is missing — do not let the process enter the call-handling path and crash mid-call.
- For each SDK, the env vars live in `sdk/patter/config.py` and `sdk-ts/src/config.ts`. Update both in lockstep when adding a new one, and add the new variable to `.env.example` in the same commit.

## Local Dev Loop (voice-call smoke test)

Unit and integration tests do not catch voice regressions. Before releasing any change to `StreamHandler`, telephony adapters, or provider adapters, place a real call against a local Patter instance end-to-end:

1. Start the embedded server. The exact command lives in the local-mode entry point in `sdk/patter/cli.py` / `sdk-ts/src/cli.ts` — likely `patter serve --local` or equivalent. Verify against the CLI module before invoking.
2. Expose the local server via `ngrok http 8080` (or whatever port the local server binds) and copy the HTTPS forwarding URL.
3. Point a Twilio or Telnyx phone number's voice webhook at `https://<ngrok-host>/voice`.
4. Place a test call to that number from a real phone.
5. Watch the logs and confirm the full chain: webhook received → `StreamHandler` created → provider adapter started → first audio byte out within the latency budget → clean call tear-down on hangup.

The "done" signal is a readable transcript line plus a `CallMetricsAccumulator` snapshot with a non-zero `first_audio_latency_ms`. If either is missing, something is wrong even if tests pass. The smoke test is mandatory — not optional — before any release or any merge that touches the real-time audio path.

Common failure modes to look for during the smoke test:

- Silent first audio — TTS connected but no bytes flowing.
- One-way audio — caller hears the agent but the agent does not hear the caller (usually a codec mismatch in the telephony adapter).
- Barge-in not interrupting playback — TTS keeps speaking when the caller starts talking.
- Delayed tear-down — call stays "active" in the dashboard after hangup.

Each of these is invisible to unit tests and obvious on a real call. Record the smoke-test call metrics (first audio latency, total turn count, any errors) in the PR description when the change touches the audio path.

## Additional Build & Test Commands

The existing `## Build & Test Commands` section covers `sdk/` and `sdk-ts/` only. These are the commands for the other surfaces. If any of these commands do not yet exist in the repo, treat them as expected commands and verify against the relevant package manifest before relying on them. TODO: confirm against `backend/pyproject.toml`, `docs/mint.json`, and the current state of `website/`.

- **Backend** (`backend/`):

        cd backend
        pip install -e ".[dev]"
        uvicorn patter_backend.main:app --reload
        alembic upgrade head
        alembic revision --autogenerate -m "describe the change"
        pytest tests/ -v

    Note: `backend/` only runs in cloud or self-hosted mode. The SDK's local mode does not need it and should never require it to start.

- **Mintlify docs site** (`docs/`):

        cd docs
        mintlify dev           # preview on http://localhost:3000
        mintlify broken-links  # run before committing; fails on dead internal links

- **Marketing website** (`website/`): static HTML, no build step. Open `website/index.html` directly in a browser, or serve it with `python3 -m http.server 8000` from inside `website/` for a stable preview URL. Any images, fonts, or scripts referenced by the page must be checked into `website/` — the site has no bundler to fetch dependencies at build time.

When adding a new top-level surface (for example a CLI package, a dashboard frontend, or a managed-agents microservice), add its build, install, and test commands here in the same PR that introduces the surface. A new surface without a build entry in this section is a documentation bug.

One-shot commands that run everything locally should also live here when they exist. If the repo has a top-level `Makefile`, `justfile`, or `npm run all`-style orchestrator that chains install, lint, test, and build across every package, document the entry point and its expected runtime so a new contributor can confirm a healthy workspace in one command.

## Provider Contracts

When adding a new STT, TTS, LLM, or telephony provider, implement the relevant protocol and register it in the provider factory. Exact file paths are likely as listed — grep for the base class name before editing to confirm. A provider PR is incomplete without matching implementations in BOTH SDKs, tests in BOTH SDKs, and reference docs for BOTH SDKs.

- **New LLM provider**: implement the `LLMProvider` protocol (likely at `sdk/patter/providers/llm/base.py` and `sdk-ts/src/providers/llm/base.ts`), register it in the provider factory, add the API key env var to the config module, add a minimal round-trip test under `tests/providers/` in both SDKs, and document the new provider on both Mintlify reference pages. The protocol must support streaming responses and tool calling — both are hard requirements for pipeline mode. A provider that only returns full responses will not work with `LLMLoop`.
- **New STT or TTS provider**: same pattern, under `providers/stt/` and `providers/tts/` in each SDK. Include codec and sample-rate assumptions in the doc page. For STT, document whether the provider supports partial transcripts (required for low-latency interruption) and word-level timestamps. For TTS, document the minimum chunk size the provider streams so barge-in interruption works reliably.
- **New telephony adapter**: implement the `TelephonyAdapter` protocol (likely at `sdk/patter/telephony/base.py` and `sdk-ts/src/telephony/base.ts`). The adapter must handle audio codec conversion to and from the internal format, webhook signature verification, and barge-in semantics (interrupting playback when the caller speaks). Twilio (mulaw 8kHz, mark-based barge-in) and Telnyx (PCM 16kHz, Ed25519 signatures) are the reference implementations — read both before adding a third.

Cross-cutting checklist for any provider PR:

1. Config env var registered in both `sdk/patter/config.py` and `sdk-ts/src/config.ts`, with a matching entry in `.env.example`.
2. Round-trip test that runs in CI against a mocked HTTP layer in both SDKs — no live network calls in the default test suite, because CI does not carry provider credentials.
3. Optional end-to-end test behind an env-gated flag for maintainers who have real credentials, so live behaviour can still be validated on demand.
4. Reference doc page added on the Mintlify site for both Python and TypeScript, with the same feature matrix and examples on each.
5. `CHANGELOG.md` `Added` entry under `Unreleased`, naming the provider and the SDK(s) affected.
6. Voice-call smoke test executed if the provider sits on the real-time audio path (any STT, TTS, or telephony adapter, and any LLM provider used in realtime mode).

A provider PR is incomplete without matching implementations in BOTH SDKs, tests in BOTH SDKs, and reference docs for BOTH SDKs.

## Lint, Format, Type-Check

Separate from test commands. Every commit must leave these clean — they run in CI and failures block merge. Run them locally before pushing. Do not `--fix` or reformat unrelated files inside a functional PR — formatting-only changes belong in a dedicated PR. If any tool name below is wrong for this repo, check `pyproject.toml` and `sdk-ts/package.json` to confirm the formatter, linter, and type-checker in use.

- **Python** (`sdk/`, `backend/`): the Python linter and formatter in use is likely `ruff`. Run `ruff check .`, `ruff format --check .`, and `mypy patter` (or `mypy .` for `backend/`). If the project uses `black` in place of `ruff format`, substitute `black --check .`.
- **TypeScript** (`sdk-ts/`): `npm run lint` (typically `tsc --noEmit`), `npm run format:check` (typically `prettier --check .`), and `npm run typecheck` if it is a separate script from `lint`.
- **Docs** (`docs/`): Mintlify performs its own content validation. Run `mintlify broken-links` before committing, and if the site uses MDX, keep component imports in alphabetical order for easy diffing.
- **Markdown** (all surfaces): no hard-coded line wrap. Keep prose on long lines; let the renderer wrap. Hard-wrapped paragraphs produce noisy diffs when a single word is added.
- **Pre-commit**: if the repo uses `pre-commit` or `husky`, let it run. Do not `--no-verify` your way around a hook failure — fix the underlying issue, re-stage, and create a fresh commit.
- **CI parity**: the local lint/format/type-check commands must match what CI runs. If CI fails on a rule your local run did not catch, update the local command in this section so the next contributor does not hit the same gap.

Running `--fix` or `--write` in the course of a functional change hides real edits inside formatting noise. Keep them separate.

If a lint or type-check rule is wrong and needs to be disabled, disable it in the config file (`pyproject.toml`, `.eslintrc`, `tsconfig.json`) with a short comment explaining why, not inline with `# noqa` or `// eslint-disable-next-line`. Inline suppressions accumulate invisibly and make the real signal harder to find. Config-level opt-outs are visible in one place and easy to audit.

Treat typing as non-negotiable. Both SDKs have strict type configuration on (`mypy --strict` equivalent for Python, `"strict": true` in `tsconfig.json`). Do not paper over type errors with `Any`, `# type: ignore`, `as unknown as T`, or `any`. If a type is genuinely wrong at a library boundary, narrow it with a typed wrapper function rather than suppressing at the call site. The goal is that a reader of any function can trust its signature.

## Release & Version Sync

Patter ships two packages from one repo: `patter` on PyPI and `patter` on npm. They must stay version-locked. Never tag a release without the voice-call smoke test passing on a real phone number against a real provider — unit tests do not prove voice works.

1. **SemVer contract**: major = breaking API change in either SDK (renamed method, removed config field, changed default that alters call behavior). Minor = additive feature in either SDK, but only once dual-SDK parity has landed. Patch = bug fix that does not change any public signature or default. If a feature lands in only one SDK temporarily, it ships behind an "experimental" flag (config field prefixed `experimental_`) and does NOT count as a minor bump until parity exists. Breaking changes require a pre-release cycle (`-rc.N`) — never a surprise major.
2. **Version sync**: `sdk/patter/__version__.py` and the `version` field in `sdk-ts/package.json` must be bumped together in the same commit. A commit that updates only one is a bug.
3. **CHANGELOG promotion**: rename the `Unreleased` block to the new version heading with today's date in `YYYY-MM-DD` form, then open a new empty `Unreleased` block on top.
4. **Tag and release**: `git tag vX.Y.Z && git push --tags`. GitHub Actions (`release.yml`) publishes to PyPI and npm. Watch the workflow until both uploads succeed — a half-published release is worse than no release.
5. **Pre-releases**: use `vX.Y.Z-rc.N` tags for release candidates. After pushing, verify that both the PyPI and npm pre-release dist-tags landed correctly before announcing. Install the pre-release into a scratch environment (`pip install patter==X.Y.Z-rc.N`, `npm install patter@X.Y.Z-rc.N`) and run the voice-call smoke test against it.
6. **Post-release**: confirm the new empty `Unreleased` block exists at the top of `CHANGELOG.md`, update the Mintlify version switcher under `docs/` if the site uses one, and cut a short release note summarizing user-visible changes.

If a published release is broken — a missing file in the wheel, a bad `package.json` export map, a regression in the real-time audio path — do not try to retroactively edit or delete the release. Yank-style deletions on PyPI and npm are limited and confusing. Instead, publish a fresh patch version (`X.Y.Z+1`) with the fix, update `CHANGELOG.md` to call out the broken version explicitly under `Fixed`, and communicate the breakage in the release note for the new version.

Reminder: never tag a release without the voice-call smoke test passing on a real phone number against a real provider. Unit tests do not prove voice works.

## Documentation Production

Documentation is a first-class deliverable, not an afterthought. Patter has three kinds of docs and every change should know which bucket it belongs in:

1. **User-facing reference** — the Mintlify site under `docs/`. This is what SDK consumers read. Every public API change (new method, new config field, new error type, changed default) must land here in the same PR as the code.
2. **Contributor docs** — this `CLAUDE.md`, per-package `CLAUDE.md` files (create one inside `sdk/`, `sdk-ts/`, `backend/`, `website/`, `examples/` if missing), and topical notes such as `RELEASE_PROCESS.md` or `VERSIONING.md` at the repo root when they do not yet exist.
3. **Implementation records** — ExecPlans, ADRs, and status snapshots under `docs/plans/`, `docs/adr/`, and `docs/status/` (create these subdirectories as needed — see `docs/ layout` below). These are internal and live alongside the code rather than in the Mintlify site.

Before any non-trivial implementation, **run the session context scan from Key Rule #6**. The goal is to understand what was decided, tried, shipped, or rejected in recent sessions so you do not re-litigate closed decisions, duplicate half-finished work, or miss context that a prior session already captured. If a prior ExecPlan touches the same surface, extend or supersede it explicitly rather than starting a parallel one. If you discover you are about to duplicate prior work, stop and reconcile with the existing plan.

Rules that apply to all three:

- Write plain prose first. Use lists and tables only when they add clarity.
- Never invent paths. If a directory does not exist yet, create it the first time you need it and note the convention here.
- Every doc that describes a moving target should carry a "Last Updated: YYYY-MM-DD" line at the top so staleness is visible.
- If a piece of tribal knowledge (a gotcha, a workaround, a test flake) has bitten you twice, write it down in the relevant package CLAUDE.md or in `docs/TROUBLESHOOTING.md`.

## Dual-SDK doc parity

Feature parity between `sdk/` and `sdk-ts/` is mirrored at the documentation layer. Every user-visible SDK feature must be documented in BOTH the Python reference and the TypeScript reference sections of the Mintlify site under `docs/`. Concretely:

- If you add a method `Patter.create_agent(...)` in `sdk/patter/client.py`, the same method and signature must appear in `sdk-ts/src/client.ts` AND both reference pages in `docs/` must be updated (or created) in the same PR.
- If defaults differ between languages (they should not, but sometimes must), call that out explicitly in both reference pages with a short explanation.
- If a feature is intentionally Python-only or TypeScript-only, state that at the top of its doc page and record the decision in the ExecPlan for that feature (see below).
- PR reviewers should reject any PR that updates only one language's reference without a documented reason.

## docs/ layout

`docs/` is dual-purpose: it hosts the Mintlify site AND the internal implementation records. Keep them separated by subdirectory so the Mintlify build does not accidentally publish internal notes.

- `docs/` (root) — Mintlify user-facing documentation. Already the source for the published site. Leave the existing structure intact.
- `docs/plans/` — ExecPlans (see ExecPlans section). Create if missing. Flat directory, file-name-sortable.
- `docs/adr/` — Architecture Decision Records. Optional. Use when an irreversible architectural choice is made (choice of provider protocol, SDK packaging change, breaking schema decision). One ADR per decision, named `YYYY-MM-DD-ADR-<slug>.md` with a status, context, decision, and consequences section.
- `docs/status/` — Dated point-in-time snapshots of per-surface health. Optional but useful for release prep. File names: `YYYY-MM-DD-STATUS-<surface>.md` (for example, `2026-04-11-STATUS-sdk-ts-release-readiness.md`).
- `docs/YYYY-MM-DD-<CATEGORY>-<slug>.md` — Flat dated notes for free-form artifacts that do not fit the above (RFCs, migration guides, deprecation notes, breaking-change announcements). CATEGORY is a short free-form word such as RFC, MIGRATION, BREAKING, DEPRECATION, RELEASE.

Make sure `docs/plans/`, `docs/adr/`, and `docs/status/` are excluded from the Mintlify build config if they end up inside the Mintlify content root. If the Mintlify site lives under a different sub-path (for example `docs/site/`), that is even better — move the internal dirs to `docs/plans/` at the repo root and leave Mintlify untouched. Pick one layout the first time this comes up and update this section to reflect it.

The root `CHANGELOG.md` is the single public record of notable changes. Keep it in Keep-a-Changelog style: one section per version, grouped by `Added`, `Changed`, `Deprecated`, `Removed`, `Fixed`, `Security`. An `Unreleased` section at the top collects entries as they land, and gets renamed on release tag.

## ExecPlans

For any feature touching 3+ files, any change with significant unknowns, or any work that will span more than one session, create a self-contained ExecPlan at:

    docs/plans/YYYY-MM-DD-PLAN-<slug>.md

An ExecPlan is a living design doc that a complete novice — with only the working tree and this file — could follow end-to-end to deliver a working, observable result. Small single-file changes do not need one.

Every ExecPlan must contain and keep up to date these sections:

- **Purpose / Big Picture** — what a user can do after this change that they could not do before, and how to see it working.
- **Progress** — a checkbox list with timestamps. Every stopping point is recorded. Partially-completed work is split into "done" and "remaining" entries, never overwritten.
- **Surprises & Discoveries** — unexpected behaviors, library quirks, and optimizer/runtime gotchas, each with short evidence.
- **Decision Log** — every non-obvious decision with rationale and date. If you change course mid-implementation, record it here.
- **Outcomes & Retrospective** — written at completion (or at major milestones). Compare the result against the original Purpose. Note gaps and lessons.

Write the plan in plain prose with narrative milestones, not a wall of checklists. State repository-relative paths explicitly. Embed any external knowledge you rely on directly in the plan — do not link out to blogs. For the full specification and rationale, the authoritative reference is `.claude/rules/plans.md` (create this file by copying the monorepo standard if you have access to it; otherwise the bullets above are sufficient).

## Code review and change-log discipline

After any non-trivial change — anything that touches public API, multiple files, a telephony adapter, a provider mode, or a security-sensitive path — do two things before marking work complete:

1. **Run a code-review agent.** Use the `code-reviewer` agent (or equivalent) on the diff. Address every CRITICAL and HIGH finding. Fix MEDIUM findings when the cost is low.
2. **Append a dated entry to `CHANGELOG.md`.** Add the entry to the `Unreleased` section under the correct group (`Added`, `Changed`, `Fixed`, etc.). Include the user-visible impact in one sentence and reference the PR or commit hash. Release tags move the `Unreleased` block under a new version heading with the release date.

Bug fixes that touch only one file and do not change any public behavior can skip the code-review agent but must still land a `Fixed` entry in the changelog.

## Agent team workflow

Non-trivial multi-file tasks — especially anything that spans `sdk/`, `sdk-ts/`, `backend/`, `docs/`, and `examples/` simultaneously — should be run as an agent team, not as a single session. Before coding:

1. `TeamCreate` with a descriptive team name.
2. `TaskCreate` for each logical work unit (typically one per package surface).
3. Spawn workers with the `Agent` tool and assign tasks via `TaskUpdate` owner.
4. Monitor through `SendMessage`; respond to blockers promptly.
5. Shut workers down cleanly when all tasks complete.

Single-file edits, quick bug fixes, and questions do NOT need a team — use one directly. The rule is: if the work touches more than two packages or has more than three logical sub-tasks, create a team.

## CI/CD

GitHub Actions (`test.yml`):
- Python: tests on 3.11, 3.12, 3.13
- TypeScript: tests on Node 18, 20, 22 + lint + build

Release (`release.yml`): triggered by `v*` tags, publishes to PyPI + npm. Before tagging a release, verify that `CHANGELOG.md` has a clean `Unreleased` section ready to promote, and that the Mintlify site under `docs/` reflects the new public API.

## gstack Skill Suite

The full [garrytan/gstack](https://github.com/garrytan/gstack) skill suite (v0.16.3.0) is installed at `~/.claude/skills/gstack` (symlinked from `~/Desktop/codes/hackathon-stanford-deepmind/gstack/`). All 36 skills are registered. Use them freely during development — key skills relevant to Patter work:

| Skill | Purpose |
|-------|---------|
| `/office-hours` | YC-style forcing questions before building. Run before `/plan-*` reviews. |
| `/autoplan` | Auto-runs CEO, design, eng, and DX reviews sequentially. |
| `/plan-ceo-review` | CEO/founder plan review with scope modes. |
| `/plan-eng-review` | Eng manager plan review: architecture, data flow, edge cases, tests. |
| `/plan-design-review` | Interactive design review, rates each dimension 0-10. |
| `/plan-devex-review` | DX plan review with developer personas and friction tracing. |
| `/ship` | Ship workflow: merge base, tests, diff review, version bump, PR. |
| `/review` | Pre-landing PR review: SQL safety, trust boundaries, side effects. |
| `/qa` | Systematic QA + bug fixing with atomic commits and regression tests. |
| `/cso` | OWASP Top 10 + STRIDE threat modeling + secrets audit. |
| `/investigate` | Systematic 4-phase root-cause debugging. |
| `/health` | Code quality dashboard with weighted 0-10 score. |
| `/checkpoint` | Save/resume working state for session handoff. |
| `/browse` | Headless browser for QA testing and dogfooding (requires `bun install && bun run build` in the gstack dir). |

Run `/gstack-upgrade` to update to the latest version.

## Hackathon: Stanford x DeepMind (April 12, 2026)

**Event:** Stanford x DeepMind Hackathon — Build. Ship. Win up to $5M in Seed Funding.
**Date:** April 12, 2026, 10:00 AM - 6:00 PM PT. 3-hour build sprint (11:30 AM - 2:30 PM). Submission at 2:30 PM sharp.
**Tracks:** Google AI Studio (Gemini) + FastShot.ai (Mobile). 40+ VCs judging.

**What we are building — two projects, one platform pitch:**

1. **ShipCall** (FastShot track) — An AI voice agent that proactively calls you when your code needs attention (build failures, deploys, decisions). Uses Patter outbound calls + tool calling. The demo: phone rings mid-pitch, AI reports a deploy issue and suggests a fix live on speaker.
2. **VoiceScope** (AI Studio track) — Snap a photo of anything, get a phone call from an AI expert walking you through what it sees. Uses Gemini 3 vision + Patter outbound calls. The demo: photo of a circuit board triggers a call explaining the issue and how to fix it.

Both share the same Patter local-mode infrastructure (Twilio, ngrok, outbound call pattern). The pitch frames Patter as a platform with two proof points.

**Judging criteria:** Technical feasibility, innovation, real-world value, market potential, social engagement (YouTube demo video likes/shares count for 2 weeks).

**Hackathon docs:** `.hackathon/` directory contains the full event breakdown, day strategy, and idea details. Read those files for the minute-by-minute sprint plan, demo scripts, risk mitigation, and social traction strategy.

**Checkpoint discipline:** At every 30-minute mark, ask "Can we demo what we have right now?" If no, stop feature work and fix until demoable. A working subset always beats a broken superset.

## Reminder

Dual-SDK parity. Agent team for non-trivial work. No AI attribution in commits. Keep `CHANGELOG.md` current. ExecPlan for anything 3+ files or with unknowns. Every generated doc filename starts with `YYYY-MM-DD-`. User-facing SDK changes land in both Python and TypeScript reference docs in the same PR. **Session context scan before non-trivial work** — `ls | sort -r | head -5` on doc dirs, Haiku summarizes each in 2-3 lines, expand to 10 if needed.
