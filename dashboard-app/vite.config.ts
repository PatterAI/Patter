import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { viteSingleFile } from 'vite-plugin-singlefile';

// Vite + React + singlefile plugin: emits a single self-contained dist/index.html
// with all JS, CSS, and assets inlined. Both SDKs (Python + TypeScript) embed
// this file as the dashboard UI served from `GET /`.
export default defineConfig({
  plugins: [react(), viteSingleFile()],
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
