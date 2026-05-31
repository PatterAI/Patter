# Python SDK — agent quickstart

This file is the per-library guide for AI agents working in `libraries/python/`. For repo-wide rules, see [`AGENTS.md`](../../AGENTS.md) and [`CONTRIBUTING.md`](../../CONTRIBUTING.md).

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
    ├── cli.py              # `getpatter` console-script entry point
    ├── local_config.py     # LocalOptions + local-mode config
    ├── models.py           # public dataclasses (frozen=True)
    ├── exceptions.py       # PatterError + ErrorCode enum
    ├── pricing.py          # PricingUnit enum + provider price tables
    ├── server.py           # FastAPI app
    ├── stream_handler.py   # per-call orchestrator
    ├── telephony/          # Twilio + Telnyx + Plivo adapters (twilio.py / telnyx.py / plivo.py / common.py)
    ├── carriers/           # carrier classes (twilio.py / telnyx.py / plivo.py)
    ├── audio/              # transcoding, pcm_mixer, background_audio
    ├── tools/              # tool_decorator, tool_executor
    ├── providers/          # voice / LLM / STT / TTS provider adapters
    ├── services/           # llm_loop, metrics, sentence_chunker, text_transforms, ivr, ...
    ├── observability/      # event_bus + OTel tracing
    ├── evals/ engines/ integrations/   # eval runner, engines, external integrations
    ├── dashboard/
    ├── llm/ tts/ stt/      # public provider namespaces (env-var auto-resolve)
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
- Logger: `logging.getLogger("getpatter")` — never `print()`. Sub-namespaces like `getpatter.providers.deepgram_stt` are used per-module.
- New config fields are optional with safe defaults (backward compat).
- Authentic tests: mock only at paid/external boundary, tag `@pytest.mark.mocked`.

## Parity with TypeScript

Every public feature in this SDK MUST exist in `libraries/typescript/` with the same defaults and error taxonomy. Run `/parity-check` before PR.
