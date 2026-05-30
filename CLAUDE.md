# CLAUDE.md

Patter is open-source telephony infrastructure that connects AI agents to phone calls. Two SDKs with full parity: Python (`pip install getpatter`) and TypeScript (`npm install getpatter`).

## Always use (not alternatives)
- Python: `pytest` (`asyncio_mode = "auto"` — no decorator needed). Not `unittest`.
- TypeScript: `vitest`, `tsup`, `npm`. Not jest, bun, pnpm.
- Logging: Python `logging.getLogger("getpatter")`, never `print()`. TS `getLogger().info/warn/error` from `src/logger.ts`, never bare `console.*` in library code.

## Structure
```
libraries/python/   Python SDK                libraries/typescript/    TypeScript SDK
docs/      Mintlify site             examples/  30 examples (developer/ + enterprise/)
tests/     Cross-SDK integration     .claude/   Agents, commands, rules, hooks
```

## Development workflow
```sh
# 1. Tests (before any change and before commit)
cd libraries/python     && pytest tests/ -v                   # Python
cd libraries/typescript && npm test                           # TypeScript

# 2. Typecheck (TS only, Python is duck-typed but `mypy` may be added)
cd libraries/typescript && npm run lint

# 3. Full parity + build before PR
/parity-check                                   # Runs sdk-parity agent
cd libraries/typescript && npm run build

# 4. Commit + push + open PR
/commit-push-pr
```

## Non-negotiable rules
1. **Feature parity** — Every feature lands in BOTH SDKs in the same PR. Run `/parity-check` before merge.
2. **Immutable config** — Python `@dataclass(frozen=True)`, TypeScript `readonly` fields.
3. **Opt-in** — New config fields must be optional with sensible defaults (backward compat).
4. **Async everywhere** — Python fully `async/await`, TS uses promises for all I/O.
5. **Provider-agnostic** — Features work across Realtime, ConvAI, and Pipeline modes where applicable.
6. **Authentic tests** — Tests exercise real code paths. Mocks ONLY at paid/external boundaries (OpenAI/ElevenLabs WS, Twilio/Telnyx carrier) and MUST be tagged `@pytest.mark.mocked` (Py) or filename `*.mocked.test.ts` (TS). No fake functions, fake results, or fake conversions. See `.claude/rules/authentic-tests.md`.
7. **No hardcoded sensitive data** — No real phone numbers, emails, customer SIDs, API keys, or public IPs in code/docs/tests. Use the placeholder table in `.claude/rules/security.md §What counts as PII` or env vars. Enforced by `.claude/hooks/scan-sensitive-on-write.sh` (PostToolUse block), `.pre-commit-config.yaml` (gitleaks), and `.githooks/pre-push` (final sweep).
8. **Plan mode for non-trivial tasks** — Before touching multiple files or changing public API, enter plan mode, identify changes + edge cases + breaking-change impact, wait for explicit user approval. Do NOT auto-add backward-compat shims — ask first.
9. **Releases via PR only — NEVER push to `main` directly.** Every code change that ships in a release MUST land via a pull request, even one-line fixes. See "Release process" below for the full workflow. Direct `git push origin main` is forbidden. Tagging a version (`git tag vX.Y.Z`) is forbidden until the corresponding code is merged via PR. The release GitHub Action only runs on tag-push, but tags must point at commits that are already on `main` via a merged PR.
10. **No competitor references or external license headers in source files.** Patter is open-source under its own MIT license — the codebase must stand alone. No `Copyright (c) <year> LiveKit/Pipecat/Cartesia/...` blocks, no `# Adapted from <competitor>` provenance comments, no competitor product names in identifiers, docstrings, error messages, or example file names. Provider integrations (OpenAI, ElevenLabs, Deepgram, Twilio, Telnyx, etc.) ARE allowed — those are dependencies we ship adapters for. See `.claude/rules/no-competitor-references.md` for the full list and exceptions.
11. **Every user-visible change updates `CHANGELOG.md` in the same commit/PR.** Append to `## Unreleased` under `### Added` / `### Changed` / `### Fixed` / `### Deprecated` / `### Removed` / `### Security`. Refactors, test-only changes, and comment-only diffs are exempt. Pre-release the `## Unreleased` block must be non-empty (otherwise either nothing user-visible changed, or entries were forgotten). Full rule: `.claude/rules/documentation-best-practices.md` (invariant 0).
12. **Never `gh pr merge --admin` to bypass a failing required check.** If `gh pr merge` is rejected with "base branch policy prohibits the merge", STOP. Read the failing check via `gh pr checks <PR#>`, classify it (real bug → request changes; flake → ship a separate fix PR and re-trigger CI; wrong workflow → fix it in its own PR). Only after the failing check is green may the merge proceed. The user must explicitly authorise an admin bypass in the current turn AND the agent must surface which check is being bypassed and why it's safe. A user saying "merge it" or "go ahead" is NOT admin-merge authorisation — that authorises the normal path that a red check should block. Full rule: `.claude/rules/never-admin-merge.md`.

## Breaking changes

Patter is published on PyPI and npm; users pin versions. Before shipping any change that alters public API surface:
1. Flag it explicitly in chat as "⚠️ breaking".
2. Offer both options: **Option A** clean break (requires user migration, cleaner long-term); **Option B** backward-compatible shim (more code, but zero migration).
3. Let the user choose. Often clean break is preferred over shim rot.
4. Document the migration path in the PR description.

## PR description format

```markdown
## Summary
[1-3 bullets, what & why]

## Implementation
- [architectural decisions]
- [files touched]
- [deps added/removed]

## Breaking change?
[No, or describe migration path]

## Test plan
- [ ] Python: `pytest tests/`
- [ ] TypeScript: `npm test` + `npm run lint` + `npm run build`
- [ ] /parity-check clean
- [ ] E2E smoke (only when pipeline/handler changes)

## Docs updates
- [list `docs/**.mdx` pages touched, or "N/A"]
```

## Release process (CRITICAL — follow exactly, every time)

The user explicitly chose this workflow after a fast-path direct push happened by mistake. Treat every step as non-skippable.

1. **Create a release branch off `main`** — e.g. `release/0.5.5`, `fix/cerebras-default`, `feat/foo`. Never edit `main` in place.
2. **Commit the change(s)** in conventional-commit style (`feat:`, `fix:`, `perf:`, `docs:`, `chore:`). Multiple independent changes split into multiple commits per the "commit-per-feature" convention.
3. **For any version bump**, change ALL THREE in the same commit:
   - `libraries/python/getpatter/__init__.py` → `__version__ = "X.Y.Z"`
   - `libraries/python/pyproject.toml` → `version = "X.Y.Z"`
   - `libraries/typescript/package.json` → `"version": "X.Y.Z"`
   Forgetting `pyproject.toml` is the most common mistake — PyPI publish silently 404s.
4. **`git push -u origin <branch>`** and **`gh pr create`** with a PR body following the template below.
5. **Wait for CI to be fully green** — Python 3.11 / 3.12 / 3.13, TypeScript 20 / 22, E2E, security tests, lint, secret scan. No merge until all checks pass.
6. **User reviews and merges** (squash-merge is the project convention). The merge is performed by the user, not by the AI agent.
7. **AFTER the PR is merged**: locally update `main` and only THEN tag.
   ```bash
   git checkout main && git pull --ff-only origin main
   git tag -a vX.Y.Z -m "Release X.Y.Z — <one-line summary>"
   git push origin vX.Y.Z
   ```
   The tag triggers `.github/workflows/release.yml` which auto-publishes to PyPI (OIDC) and npm (token).
8. **Create the GitHub Release annotation** via `gh release create vX.Y.Z` with notes that link the merged PR.

**Never:**
- `git push origin main` with new code (even for trivial fixes — there is no fast path).
- `git tag vX.Y.Z` before the corresponding code is merged on `main` via PR.
- Run `twine upload` / `npm publish` / `npm run publish` locally.
- Skip step 3's `pyproject.toml` bump.
- Treat "small fix" as an excuse to skip the PR. CI catches things local tests don't.

If the AI agent finds itself about to direct-push or pre-tag, it MUST stop and create a branch instead. If the user asks for a fast direct push: clarify first — they almost certainly mean "do it via PR, just quickly", not "skip the PR".

## Architecture (one-liners)
- **Mode**: Local (embedded FastAPI/Express) — Patter Cloud (`wss://api.getpatter.com`) was removed in 0.5.3 and will return as a feature in a future release. Calling `Patter(api_key=...)` raises `NotImplementedError` until then.
- **Voice**: Realtime (OpenAI all-in-one) · ConvAI (ElevenLabs) · Pipeline (STT→LLM→TTS modular).
- **Telephony**: Twilio (mulaw 8kHz, TwiML) · Telnyx (PCM 16kHz, Call Control, Ed25519 verify).
- **Key files**: `client.py/ts` (entry) · `stream_handler.py/ts` (per-call) · `services/llm_loop.py/ts` (pipeline + tools, sampling kwargs lifted to `OpenAILLMProvider` parent in 0.5.3) · `services/metrics.py/ts` (cost) · `dashboard/store.py/ts` (500-call ring buffer).
- **Built-in tools**: `transfer_call`, `end_call` auto-injected into every agent.

## Cerebras default model

Default = `gpt-oss-120b` (set in 0.5.4 — see `libraries/python/getpatter/providers/cerebras_llm.py:_DEFAULT_MODEL` and `libraries/typescript/src/providers/cerebras-llm.ts:DEFAULT_MODEL`). On Cerebras WSE-3 hardware all model sizes saturate the downstream TTS consumption rate (~150-300 tok/sec), so a 120B model adds no realtime latency over an 8B one. `gpt-oss-120b` runs at ~3000 tok/sec and has no scheduled deprecation. `llama3.1-8b` retires 2026-05-27.

If a future audit recommends switching the default to a smaller model "for free-tier safety": bring it up explicitly with the user before changing. The decision was made deliberately based on Cerebras's published throughput chart.

## Latency-reduction TODO

Latency-reduction work is tracked internally. Coordinate with maintainers before opening another latency pass so you don't duplicate items that have already been audited.

## Agents (invoke with `@name` or via Task tool)
- `sdk-parity` — detect/fix Python↔TS gaps (canonical)
- `build-validator` — run tests + lint + build for both SDKs
- `code-simplifier` — post-work cleanup pass
- `provider-reviewer` — review new/changed voice provider integrations
- `telephony-reviewer` — review Twilio/Telnyx adapter changes (audio, webhooks)
- `example-validator` — verify `examples/` still runnable after SDK changes

## References
- Architecture deep-dive: `docs/ARCHITECTURE.md`
- Release process: `docs/RELEASE.md`
- Rules: `.claude/rules/`
- Slash commands: `.claude/commands/`
