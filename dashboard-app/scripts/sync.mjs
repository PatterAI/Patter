#!/usr/bin/env node
// Copy dashboard-app/dist/index.html into both SDKs after `vite build`.
// Both SDKs embed the same HTML file (single source of truth).
import { copyFileSync, existsSync, mkdirSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, '..', '..');
const src = resolve(repoRoot, 'dashboard-app', 'dist', 'index.html');

if (!existsSync(src)) {
  console.error(`[sync] ${src} not found — run 'npm run build' first.`);
  process.exit(1);
}

const targets = [
  resolve(repoRoot, 'libraries', 'typescript', 'src', 'dashboard', 'ui.html'),
  resolve(repoRoot, 'libraries', 'python', 'getpatter', 'dashboard', 'ui.html'),
];

for (const dest of targets) {
  mkdirSync(dirname(dest), { recursive: true });
  copyFileSync(src, dest);
  console.log(`[sync] ${dest}`);
}
console.log('[sync] done — TS and Python SDKs now serve the latest dashboard.');
