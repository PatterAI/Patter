# Contributing to Patter

Thank you for your interest in contributing to Patter!

## Development Setup

### Python SDK
```bash
cd sdk-py
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest tests/ -v
```

> Note: the legacy `[local]` extra is now an empty alias kept only for backwards compatibility — `[dev]` is sufficient.

### TypeScript SDK
```bash
cd sdk-ts
npm install
npm test
npm run build
```

## Pull Request Process

1. Fork the repository
2. Create a feature branch: `git checkout -b feat/my-feature`
3. Write tests first (TDD)
4. Implement the feature
5. Ensure all tests pass: `pytest tests/ -v` / `npm test`
6. Commit with conventional commits: `feat:`, `fix:`, `docs:`
7. **Validate locally**: `bash scripts/pr-validate.sh` (see below)
8. Open a Pull Request against `main`

## Pre-PR validation

Run every PR-blocking CI check locally before opening the PR:

```bash
bash scripts/pr-validate.sh           # default: ~3-5 min
bash scripts/pr-validate.sh --quick   # pre-commit + notebooks (~30s)
bash scripts/pr-validate.sh --full    # default + e2e + all-extras (~10 min)
```

The script mirrors `.github/workflows/test.yml` and `notebooks.yml` so a
green local run lines up with green CI. Selective skips: `--skip-py`,
`--skip-ts`, `--skip-notebooks`, `--no-stop`.

First-time setup:

```bash
pip install pre-commit==3.8.0          # required
brew install gitleaks                   # optional — system fallback
```

If pre-commit's bundled tools don't work on your machine (hardened macOS,
Go OOM during gitleaks build), use the documented escape hatches:

```bash
PR_VALIDATE_SKIP_GITLEAKS=1 PR_VALIDATE_SKIP_NBSTRIPOUT=1 bash scripts/pr-validate.sh
```

CI always runs the full unaltered suite, so these only affect local runs.

## Code Style

### Python
- Follow PEP 8
- Use type hints on all public methods
- Use `logging.getLogger("patter")` — never `print()`
- Frozen dataclasses for models
- Async everywhere — no blocking I/O

### TypeScript
- Strict TypeScript — no `any`
- Use `WebSocket.OPEN` not magic numbers
- Export all public types from `index.ts`
- `xmlEscape()` for all TwiML strings

## Adding a New Provider

Patter uses an **instance-based class pattern** (post-0.5.0). To add a new STT, TTS, or LLM provider:

1. Create the provider class:
   - Python: `sdk-py/getpatter/stt/<name>.py`, `sdk-py/getpatter/tts/<name>.py`, or `sdk-py/getpatter/llm/<name>.py` exporting a class named `STT`, `TTS`, or `LLM`.
   - TypeScript: `sdk-ts/src/stt/<name>.ts`, `sdk-ts/src/tts/<name>.ts`, or `sdk-ts/src/llm/<name>.ts` exporting `STT`, `TTS`, or `LLM`.
2. Read credentials from the standard env var (e.g. `<NAME>_API_KEY`) when no `api_key` / `apiKey` is passed; throw a clear error when both are missing.
3. Re-export a flat alias from the package barrel (`getpatter/__init__.py` for Python, `sdk-ts/src/index.ts` for TypeScript) — for example `STT as DeepgramSTT`.
4. Wire the new class into the pipeline dispatch (stream handler) for end-to-end audio flow.
5. Add a default pricing entry under `DEFAULT_PRICING` so users see real cost numbers in the dashboard.
6. Add unit + integration tests; aim for 80%+ coverage on the new module.
7. Update the docs: `docs/{python,typescript}-sdk/{stt,tts,llm}.mdx` and the per-provider page under `docs/{python,typescript}-sdk/providers/<name>.mdx` if applicable.

## Reporting Issues

- Use the issue templates
- Include SDK version, Node/Python version, OS
- Enable debug logging: `logging.getLogger("patter").setLevel(logging.DEBUG)`
