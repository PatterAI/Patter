# TypeScript SDK — agent quickstart

This file is the per-library guide for AI agents working in `libraries/typescript/`. For repo-wide rules, see the root `CLAUDE.md` and `.claude/rules/`.

## Layout

```
libraries/typescript/
├── package.json            # npm pkg metadata, scripts, deps
├── tsconfig.json
├── tsup.config.ts          # bundler config (esm + cjs + types)
├── vitest.config.ts        # test runner config
├── .env.example
├── README.md               # user-facing quickstart
├── tests/                  # vitest suite (unit / integration / security / e2e / soak)
│   └── setup.ts
└── src/                    # the published package (`npm install getpatter`)
    ├── index.ts            # public entry — re-exports
    ├── client.ts           # Patter entry point
    ├── types.ts            # public interfaces (readonly)
    ├── errors.ts           # PatterError + ErrorCode enum
    ├── pricing.ts          # PricingUnit + provider price tables
    ├── server.ts           # Express app
    ├── stream-handler.ts   # per-call lifecycle
    ├── telephony/          # Twilio + Telnyx adapters (twilio.ts / telnyx.ts)
    ├── audio/              # transcoding, background-audio
    ├── tools/              # tool-decorator
    ├── providers/          # voice / LLM / STT / TTS providers
    ├── services/           # call-log, ivr (mostly top-level files in src/)
    ├── observability/
    ├── dashboard/
    ├── tts/ stt/           # public namespaces (env-var auto-resolve)
    └── ...
```

## Daily commands

```bash
cd libraries/typescript
npm test                                 # vitest run
npm run lint                             # tsc --noEmit
npm run build                            # tsup → dist/
npx vitest tests/server.test.ts          # one file
```

## Conventions (project-wide, restated for convenience)

- Vitest, not jest. tsup, not tsc-build. npm, not pnpm/bun.
- Public interfaces use `readonly` on every field. `readonly T[]` for arrays.
- Async everywhere — return `Promise<T>` for I/O. No `setTimeout` polling.
- Logger: `getLogger().info/warn/error` from `src/logger.ts`. Never bare `console.*` in library code.
- New config fields are optional with safe defaults (backward compat).
- Authentic tests: mock only at paid/external boundary, file suffix `*.mocked.test.ts`.

## Parity with Python

Every public feature in this SDK MUST exist in `libraries/python/` with the same defaults and error taxonomy. Naming maps `camelCase` ↔ `snake_case`. Run `/parity-check` before PR.
