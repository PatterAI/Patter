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
    ├── exceptions.py       # PatterError + ErrorCode enum
    ├── pricing.py          # PricingUnit enum + provider price tables
    ├── server.py           # FastAPI app
    ├── stream_handler.py   # per-call orchestrator
    ├── telephony/          # Twilio + Telnyx adapters (twilio.py / telnyx.py / common.py)
    ├── audio/              # transcoding, pcm_mixer, background_audio
    ├── tools/              # tool_decorator, tool_executor
    ├── providers/          # voice / LLM / STT / TTS providers
    ├── services/           # llm_loop, metrics, sentence_chunker, text_transforms, ivr, ...
    ├── observability/      # event_bus + OTel tracing
    ├── dashboard/
    ├── tts/ stt/           # public namespaces (env-var auto-resolve)
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
