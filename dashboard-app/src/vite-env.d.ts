/// <reference types="vite/client" />

// Injected at build time by Vite `define` (see ../vite.config.ts).
// Holds the SDK version read from libraries/typescript/package.json so
// the dashboard label tracks the published SDK without a manual edit.
declare const __SDK_VERSION__: string;
