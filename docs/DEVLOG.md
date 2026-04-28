# Patter — Development Log

Newest entries at the top.

---

### [2026-04-28] — Notebook Docker bootstrap: code-review fixes

**Type:** fix
**Branch:** feat/notebook-docker-launcher

**What it does:**

Addresses every CRITICAL/HIGH/MEDIUM finding from the multi-agent review of the
initial Docker launcher commit:

- **Loopback-only port maps** in `docker-compose.yml` (`127.0.0.1:8888:8888` /
  `127.0.0.1:8765:8765`) so JupyterLab is never exposed to the LAN.
- **Generated auth token by default.** `_setup._generate_jupyter_token()` writes
  a 32-byte URL-safe secret to `~/.config/patter-notebooks/jupyter_token` (mode
  `0o600`) on first run; subsequent runs reuse it. `start_docker()` injects it
  via `JUPYTER_TOKEN` env var; the Dockerfile `CMD` reads it from env. Empty
  token requires explicit opt-in via `PATTER_NOTEBOOKS_NO_TOKEN=1`.
- **Non-root container.** Dockerfile creates `patter` (UID 1000 by default,
  override via `PUID`/`PGID` build args) and drops `--allow-root`.
- **Pinned deps.** New `examples/notebooks/python/requirements.txt` locks every
  top-level dep at an exact version; Dockerfile installs from it.
- **`start_docker() -> bool`.** Every early-return now returns False so
  notebook callers can branch; `subprocess.run(..., check=False,
  capture_output=True)` surfaces stdout/stderr on compose failure instead of
  swallowing them.
- **`detach=False` guard.** Refuses to launch (would hang the kernel) and
  returns False with a banner pointing at the terminal.
- **Robust env-var truthiness.** `in_docker()` accepts `1/true/yes/on`
  (case-insensitive), not just literal `"1"`.
- **Honest docstring.** `_setup.py` module docstring acknowledges the
  TS-parity gap on the Docker helpers (tracked separately).
- **Unit tests.** `tests/test_docker_bootstrap.py` covers truthy/falsy env
  parsing, `/.dockerenv` marker, every `start_docker()` early-return branch,
  command argv assembly, and token persistence (19 tests, all green).

**Implementation details:**

- Generated token persists across `compose down`/`up` cycles so users don't
  need to re-bookmark the URL after a restart.
- Top-level imports hoisted: `secrets` and `webbrowser` now at the top of
  `_setup.py` (PEP 8). All other late imports stayed because they live with
  domain-specific helpers further down the file.
- Tests mock at the subprocess + filesystem boundary only; real Path
  resolution and real argv assembly run unchanged.

**Files changed:**

| File | Change |
|------|--------|
| `examples/notebooks/python/Dockerfile` | Non-root user, requirements.txt, JUPYTER_TOKEN from env |
| `examples/notebooks/python/docker-compose.yml` | Loopback ports, JUPYTER_TOKEN passthrough, PUID/PGID args |
| `examples/notebooks/python/requirements.txt` | New — pinned top-level deps |
| `examples/notebooks/python/_setup.py` | bool return, captured stderr, token gen, robust truthiness, detach guard |
| `examples/notebooks/python/tests/test_docker_bootstrap.py` | New — 19 unit tests |

**Tests added:**

- `examples/notebooks/python/tests/test_docker_bootstrap.py` — 19 cases.

**Breaking changes:** None — the launcher is still an opt-in commented cell.

**Docs to update:**

- [ ] Tracking issue for TypeScript notebook Docker launcher (filed separately).

---

### [2026-04-28] — Notebook Docker bootstrap: optional in-cell launcher

**Type:** feat
**Branch:** feat/notebook-series-skeleton

**What it does:**

Lets users run the Python notebook series inside a containerised JupyterLab
without leaving the notebook. Two new scoped files (`Dockerfile`,
`docker-compose.yml`) under `examples/notebooks/python/` build a Python 3.13
image with `getpatter` (pinned to `PATTER_VERSION`, default 0.5.4), the helper
deps from `pyproject.toml`, and JupyterLab. Compose mounts the parent
`examples/notebooks/` tree at `/notebooks` so `_setup.py` still finds `.env`
and `fixtures/`; ports 8888 (Lab) and 8765 (EmbeddedServer for T2/T4 cells)
are published. `env_file` is marked `required: false` so §1 cells run with
zero keys.

`_setup.py` gains two helpers: `in_docker()` (checks
`PATTER_NOTEBOOKS_IN_DOCKER=1` and `/.dockerenv`) and `start_docker(*, build,
detach, open_url)` which shells out to `docker compose up -d --build` from the
notebooks dir, no-ops when already inside the container or when the `docker`
CLI is absent. Each of the 12 Python notebooks gets an optional markdown +
commented code cell at the top — Run All on a fresh checkout still behaves
identically because the launcher is commented by default.

**Implementation details:**

- Container detection prefers an explicit env var over `/.dockerenv` so future
  rootless/podman-equivalent setups can opt in by exporting the same flag.
- Insertion script is idempotent (skips notebooks that already contain the
  `## Optional: run in Docker` marker), safe to re-run after future notebook
  edits.
- Compose uses the v2 `env_file: [{path, required: false}]` form to avoid
  failing when users haven't created `.env` yet.

**Files changed:**

| File | Change |
|------|--------|
| `examples/notebooks/python/Dockerfile` | New — Python 3.13-slim + getpatter + JupyterLab + helpers |
| `examples/notebooks/python/docker-compose.yml` | New — builds image, mounts `../`, optional `.env`, ports 8888/8765 |
| `examples/notebooks/python/_setup.py` | Added `in_docker()` and `start_docker()`; exposed `PYTHON_NOTEBOOKS_DIR` |
| `examples/notebooks/python/01_quickstart.ipynb` … `12_security.ipynb` | Inserted optional Docker markdown + code cell at the top of every notebook |

**Tests added:** None — bootstrap is a no-op until uncommented, and existing
notebook tests cover the helper module via import. Manual smoke: `docker
compose config` validates and `python -c "import _setup; _setup.in_docker()"`
returns False on host.

**Breaking changes:** None.

**Docs to update:**

- [ ] `examples/notebooks/README.md` — add a "Run in Docker" section pointing
      at the optional cell + compose file.

---

### [2026-04-27] — Notebook series Phase 5: Polish — README, launcher, drift-cron

**Type:** docs / chore
**Branch:** feat/notebook-series-skeleton

**What it does:**

Finishes the notebook series with user-facing polish:
- Rewrote `examples/notebooks/README.md` — series overview, quickstart for both
  Python and TypeScript, full key matrix table (which env var unlocks which
  cells per tier), per-topic description table, troubleshooting guide (missing
  keys, Deno kernel install, ngrok setup, live-call checklist).
- Updated `examples/notebooks/RELEASES.md` — structured run-log template with
  instructions and a "Known issues by release" section.
- Added `scripts/run_all_notebooks.sh` — headless execution of all 24 notebooks
  via `jupyter nbconvert --execute`; strips outputs after each run; non-zero exit
  if any notebook fails.
- Hooked `check_notebook_parity.py` into the daily `docs-feature-drift` cron
  workflow so Python↔TypeScript structure drift surfaces as a `docs-drift` issue
  even without a PR.

**Files changed:**

| File | Change |
|------|--------|
| `examples/notebooks/README.md` | Comprehensive rewrite |
| `examples/notebooks/RELEASES.md` | Structured template |
| `scripts/run_all_notebooks.sh` | Headless launcher (new) |
| `.github/workflows/docs-feature-drift.yml` | `notebook-parity` job added |

**Breaking changes:** None.

**Docs to update:** None — README is the doc.

---

### [2026-04-27] — Notebook series Phase 4: §3 Live Appendix everywhere

**Type:** feat
**Branch:** feat/notebook-series-skeleton

**What it does:**

Populates §3 (Live Appendix, T4) in all 24 notebooks. Every notebook now has
a real-call cell gated behind `ENABLE_LIVE_CALLS=1`: pre-flight checklist +
live call via `await p.call(env.target_number, agent=agent, first_message=...,
ring_timeout=env.max_call_seconds)`, in a `try/finally` that sweeps leftover
calls on teardown.

Topic 07 (Telnyx) uses `carrier=Telnyx(api_key=..., public_key=...)`.
Topic 10 (Advanced) uses `schedule_once(when, lambda: ...)` + `asyncio.sleep(8)`.
Topic 09 (Guardrails) demonstrates a live guardrail blocking "competitor" mid-call.
Topic 12 (Security) verifies HMAC-SHA1 on every inbound webhook during the call.

**Files changed:**

| File | Change |
|------|--------|
| `scripts/appendix_cells_01.py` – `appendix_cells_12.py` | 12 new files |
| `scripts/inject_live_appendix.py` | Phase 4 driver |
| `examples/notebooks/{python,typescript}/[01-12]_*.ipynb` | §3 populated |

**Tests added:** None (T4 cells are manually exercised only).

**Breaking changes:** None.

**Docs to update:**
- [x] `examples/notebooks/README.md` — updated in Phase 5.

---

### [2026-04-27] — Notebook series Phase 3: §2 Feature Tour everywhere

**Type:** feat
**Branch:** feat/notebook-series-skeleton

**What it does:**

Populates §2 (Feature Tour, T1+T2+T3) in all 24 notebooks — the exhaustive
layer covering every public feature and every supported provider. Each T3 cell
is wrapped with `with _setup.cell(name, tier=3, required=[...], env=env) as ok`
so missing keys yield a yellow skip banner instead of an exception.

Also fixed a critical `inject_section.py` idempotency bug: the old tag-based
deduplication left stale markdown cells on re-injection, causing heading
accumulation. Rewrote to range-based replacement using `_next_section_idx`
(skips cells whose first line starts with the current `## §N` marker, stops
at the first different `## §` heading).

**Implementation details:**

- `_next_section_idx(cells, after, current_marker)` — key function that makes
  the injector truly idempotent; two-level fix: range-based replacement +
  marker-aware end detection.
- Section heading parity required careful matching: Python `### Heading` must
  equal TypeScript `### Heading` byte-for-byte (parity checker compares first
  source line only). Fixed 4 heading mismatches across sections 04, 06, 08, 12.
- Added missing TS cells in section_cells_04 (OpenAI TTS live), section_cells_06
  (Twilio sig invalid), section_cells_08 (tool inline), section_cells_12
  (Twilio sig guard) to reach 12/12 parity.

**Files changed:**

| File | Change |
|------|--------|
| `scripts/inject_section.py` | Range-based idempotent injection (critical fix) |
| `scripts/inject_feature_tour.py` | Phase 3 driver |
| `scripts/section_cells_01.py` – `section_cells_12.py` | 12 files, §2 cells |
| `examples/notebooks/{python,typescript}/[01-12]_*.ipynb` | §2 populated |

**Tests added:**
- `scripts/test_inject_section.py` — idempotency tests added for range-based logic

**Breaking changes:** None.

**Docs to update:**
- [x] `examples/notebooks/README.md` — updated in Phase 5.

---

### [2026-04-27] — Notebook series Phase 2: Quickstart everywhere

**Type:** feat
**Branch:** feat/notebook-series-skeleton

**What it does:**

Populates §1 (Quickstart, T1+T2 only) in all 24 notebooks. Every Python
notebook now executes headless with **zero API keys** in <10s and prints a
clean status line for each cell.

The §1 cells exercise: SDK version sanity, local-mode `Patter` construction
with a Twilio carrier, cloud-mode construction with `api_key=`, and the
three engine types (OpenAI Realtime / ElevenLabs ConvAI / Pipeline) via
`p.agent(engine=...)`.

**Implementation details:**

- Cells use the `with _setup.cell(...) as ok: if ok: body` pattern (the
  Phase 1 design fix — `@contextmanager` cannot suppress the body once
  yielded).
- All cells import from `getpatter` (the actual package name), not `patter`.
  The plan's original `patter.*` imports were speculative and got corrected
  here.
- `Patter` constructor uses `carrier=Twilio(...)` / `carrier=Telnyx(...)`,
  not the older `twilio_sid=` flat kwargs.
- `_section_titles` in `check_notebook_parity.py` now compares only the
  first line of each markdown cell (the `### Heading`), not the full
  multi-line body — descriptive prose can legitimately differ between
  Python (`api_key=`) and TypeScript (`apiKey`).
- `PATTER_VERSION` pin bumped 0.5.2 → 0.5.4 (latest published SDK).

**Files changed:**

| File | Change |
|------|--------|
| `scripts/quickstart_cells.py` | Canonical §1 cell sequence (py + ts) |
| `scripts/inject_section.py` | Idempotent cell injection helper |
| `scripts/inject_quickstart.py` | Driver that injects into all 24 notebooks |
| `scripts/check_notebook_parity.py` | First-line-only heading comparison |
| `examples/notebooks/{python,typescript}/[01-12]_*.ipynb` | §1 populated |
| `examples/notebooks/.env.example` | Pin bumped to 0.5.4 |
| `examples/notebooks/python/_setup.py` | Default version pin → 0.5.4 |
| `examples/notebooks/typescript/_setup.ts` | Default version pin → 0.5.4 |

**Tests added:**

- `scripts/test_quickstart_cells.py` — 5 tests
- `scripts/test_inject_section.py` — 3 tests

**Acceptance:**

- All 8 new tests + 29 from Phase 1 + 11 TS tests green.
- Parity check green across all 12 pairs.
- `nbclient` headless run of all 12 Python notebooks succeeds with 0 keys
  set (every cell skips cleanly or prints the green status banner).

**Breaking changes:** None.

**Docs to update:** None.

---

### [2026-04-24] — Notebook series Phase 1: Skeleton

**Type:** feat
**Branch:** feat/notebook-series-skeleton

**What it does:**

Lays the foundation for a 24-notebook tutorial series under
`examples/notebooks/`. No notebook content yet — Phase 1 ships only the
infrastructure: fixture generator, shared `_setup.{py,ts}` helpers, 24
empty scaffolds (12 topics × Python + TypeScript) with `§1 / §2 / §3`
section markers, parity-check script, and CI workflow.

The Python `_setup.py` exposes `NotebookEnv`, `load`, `has_key`,
`print_key_matrix`, `cell` (context manager that yields `should_run`),
`load_fixture` (with PII redaction guard), `run_stt`, `run_tts`,
`hangup_leftover_calls`, plus the `NotebookSkip` / `skip` / `skip_section`
helpers. The TypeScript `_setup.ts` mirrors the same shape.

**Implementation details:**

- `_setup.cell` yields a `should_run` boolean rather than raising on skip,
  so cells write `with cell(...) as ok: if ok: ...body`. This is the
  cleanest way to make `with`-style cell guards work — `@contextmanager`
  cannot suppress the body once the context manager has yielded.
- Audio fixtures are synthesised via pure Python (no gTTS / pydub deps,
  which need ffmpeg). `audioop` works on this Python 3.14. Replace the
  two `hello_world_*` clips with real speech (gTTS or Piper) before
  Phase 3 STT cells need to verify transcripts.
- Pre-commit `detect-private-key` excludes the test Ed25519 keypair
  under `examples/notebooks/fixtures/keys/telnyx_test_ed25519_*.pem`
  (Telnyx signature roundtrip needs a committed test keypair).

**Files changed:**

| File | Change |
|------|--------|
| `examples/notebooks/.env.example` | All env vars grouped by tier |
| `examples/notebooks/README.md`, `RELEASES.md` | Series overview |
| `examples/notebooks/fixtures/{audio,webhooks,keys}/` | Generated fixtures |
| `examples/notebooks/python/_setup.py` | Shared helpers (~270 lines) |
| `examples/notebooks/python/pyproject.toml` | Notebook helpers package |
| `examples/notebooks/python/tests/test_setup.py` | 18 unit tests |
| `examples/notebooks/typescript/_setup.ts` | TS mirror (~270 lines) |
| `examples/notebooks/typescript/{package.json,tsconfig.json,vitest.config.ts}` | TS package |
| `examples/notebooks/typescript/tests/setup.test.ts` | 11 unit tests |
| `examples/notebooks/{python,typescript}/[01-12]_*.ipynb` | 24 empty scaffolds |
| `scripts/generate_notebook_fixtures.py` | One-shot fixture generator |
| `scripts/scaffold_notebook.py`, `scaffold_all_notebooks.sh` | Empty-notebook builder |
| `scripts/check_notebook_parity.py` | Section-title parity diff |
| `scripts/scan_notebook_secrets.py` | Pre-commit secret scan inside ipynb JSON |
| `scripts/test_*.py` | 16 tests across fixture/scaffold/parity helpers |
| `.github/workflows/notebooks.yml` | parity + outputs-stripped + setup-tests jobs |
| `.pre-commit-config.yaml` | nbstripout + secret-grep + key whitelist |

**Tests added:**

- `examples/notebooks/python/tests/test_setup.py` — 18 unit tests
- `examples/notebooks/typescript/tests/setup.test.ts` — 11 unit tests
- `scripts/test_generate_notebook_fixtures.py` — 3 tests
- `scripts/test_scaffold_notebook.py` — 5 tests
- `scripts/test_check_notebook_parity.py` — 3 tests

All green locally (Python 3.14 + Node 24); CI on Python 3.11 / Node 20.

**Breaking changes:** None — examples-only addition.

**Docs to update:**

- [ ] `examples/notebooks/README.md` will get the full key matrix and
      troubleshooting in Phase 5 polish.
- [ ] Real-speech audio fixtures regenerated before Phase 3 STT cells.
