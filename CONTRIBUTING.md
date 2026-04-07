# Contributing to Patter

Thank you for your interest in contributing to Patter!

## Development Setup

### Python SDK
```bash
cd sdk
python -m venv .venv
source .venv/bin/activate
pip install -e ".[local,dev]"
pytest tests/ -v
```

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
7. Open a Pull Request against `main`

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

1. Create adapter in `providers/` implementing the base interface
2. Add factory function in `providers/__init__.py` / `providers.ts`
3. Update handler to support the new provider
4. Add tests
5. Update docs

## Reporting Issues

- Use the issue templates
- Include SDK version, Node/Python version, OS
- Enable debug logging: `logging.getLogger("patter").setLevel(logging.DEBUG)`
