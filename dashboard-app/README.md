# Patter dashboard SPA

Vite + React + TypeScript. Bundled by `vite-plugin-singlefile` into one
self-contained `dist/index.html` (JS, CSS, assets all inlined) which both the
Python and TypeScript SDKs embed as the dashboard UI served from `GET /`.

## Scripts

```bash
cd dashboard-app

npm install
npm run dev          # vite dev server :5173, proxies /api → 127.0.0.1:8000
npm run build        # → dist/index.html (single file)
npm run sync         # copy dist/index.html into both SDKs
```

`npm run build && npm run sync` is the canonical pre-publish step. Both SDKs
keep `ui.html` checked in so users installing from PyPI/npm don't need Node.

## Layout

```
dashboard-app/
├── package.json
├── vite.config.ts        # singlefile plugin config
├── tsconfig.json
├── index.html            # vite entry
├── scripts/sync.mjs      # post-build copy to both SDKs
└── src/
    ├── main.tsx          # React root
    ├── App.tsx           # composes the dashboard
    ├── components/       # Topbar, MetricCard, CallTable, LiveCallPanel, ...
    ├── hooks/            # useCalls, useAggregates, useSSE
    ├── lib/              # field mappers (SDK shapes → UI shapes)
    └── styles/           # tokens.css, dashboard.css
```

## Why a separate sub-project

The dashboard ships as a single immutable HTML file. Customers don't run a
build step — we do, in CI, before publishing. The SDK serves the file as a
string from Express/FastAPI's `GET /` route, identical user experience to
today's vanilla template.

This sub-project's `node_modules` are dev-only and are never published.
