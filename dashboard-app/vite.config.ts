import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { viteSingleFile } from 'vite-plugin-singlefile';

// Auto-derive the SDK version label rendered in the dashboard from
// `libraries/typescript/package.json` so a bump of the published SDK
// flows into the bundled UI without a second manual edit. The TS and
// Python SDK versions are kept in lockstep (release-via-pr rule), so
// reading either one is sufficient — we pick the TS package because
// it lives in the same JS toolchain as this build.
const here = fileURLToPath(new URL('.', import.meta.url));
const sdkPkgPath = resolve(here, '..', 'libraries', 'typescript', 'package.json');
const sdkPkg = JSON.parse(readFileSync(sdkPkgPath, 'utf8')) as { version: string };

// Vite + React + singlefile plugin: emits a single self-contained dist/index.html
// with all JS, CSS, and assets inlined. Both SDKs (Python + TypeScript) embed
// this file as the dashboard UI served from `GET /`.
export default defineConfig({
  plugins: [react(), viteSingleFile()],
  define: {
    __SDK_VERSION__: JSON.stringify(sdkPkg.version),
  },
  build: {
    target: 'es2020',
    cssCodeSplit: false,
    assetsInlineLimit: 100_000_000, // inline everything
    rollupOptions: {
      output: { inlineDynamicImports: true },
    },
    outDir: 'dist',
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    // Proxy dashboard API to a locally running Patter SDK during dev (start
    // any example via `phone.serve()` on :8000 and the SPA hot-reloads against it).
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
  },
});
