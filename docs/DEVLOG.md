# Patter — Development Log

Newest entries at the top.

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
