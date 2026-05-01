# Python SDK — agent quickstart

This file is the per-library guide for AI agents working in `libraries/python/`. For repo-wide rules, see the root `CLAUDE.md` and `.claude/rules/`.

## Layout

```
libraries/python/
├── pyproject.toml          # package metadata, deps, pytest config
├── .env.example            # env vars for local runs
├── README.md               # user-facing quickstart
├── tests/                  # pytest suite (unit / integration / security / soak)
│   └── conftest.py
└── getpatter/              # the published package (`pip install getpatter`)
    ├── __init__.py
    ├── client.py           # Patter entry point
    ├── models.py           # public dataclasses (frozen=True)
    ├── exceptions.py
    ├── pricing.py
    ├── server.py           # FastAPI app
    ├── handlers/           # per-call lifecycle, telephony adapters
    ├── providers/          # voice / LLM / STT / TTS providers
    ├── services/           # llm_loop, metrics, transcoding, etc.
    ├── observability/
    ├── dashboard/
    ├── tts/ stt/
    └── ...
```

## Daily commands

```bash
cd libraries/python
pytest tests/ -v                       # all tests
pytest tests/ -m "not soak" -q         # default CI run
pytest tests/test_client.py -v         # one file
pip install -e ".[dev]"                # editable install for development
```

## Conventions (project-wide, restated for convenience)

- pytest with `asyncio_mode = "auto"` — write `async def test_*`, no decorator needed.
- Public dataclasses are `@dataclass(frozen=True)`. Tuples, not lists.
- Async I/O everywhere. `httpx.AsyncClient`, `websockets.connect`. No `time.sleep`.
- Logger: `logging.getLogger("patter")` — never `print()`.
- New config fields are optional with safe defaults (backward compat).
- Authentic tests: mock only at paid/external boundary, tag `@pytest.mark.mocked`.

## Parity with TypeScript

Every public feature in this SDK MUST exist in `libraries/typescript/` with the same defaults and error taxonomy. Run `/parity-check` before PR.
