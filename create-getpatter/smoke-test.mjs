#!/usr/bin/env node

/**
 * Tiny dependency-free smoke test for the `create-getpatter` shim.
 *
 * It builds a throwaway project dir with a locally-installed `getpatter`
 * (linked from the freshly-built TypeScript SDK) so the shim's local-CLI
 * resolution path is exercised without any network access, then asserts:
 *
 *   1. `create-getpatter --help` forwards to the wizard and prints its help.
 *   2. `create-getpatter my-app --yes ...` promotes the positional to
 *      `--name my-app` and scaffolds a runnable project (src/index.ts,
 *      package.json named `my-app`, `.env` at mode 0600).
 *
 * Run: `node create-getpatter/smoke-test.mjs` from the repo root after
 * `cd libraries/typescript && npm run build`.
 *
 * Exits 0 on success, non-zero (with a message) on the first failed check.
 */

import { spawnSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, cpSync, copyFileSync, symlinkSync, existsSync, statSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = dirname(HERE);
const TS_SDK = join(REPO_ROOT, 'libraries', 'typescript');
const SHIM = join(HERE, 'index.js');

function fail(msg) {
  console.error(`FAIL: ${msg}`);
  process.exit(1);
}

const distCli = join(TS_SDK, 'dist', 'cli.js');
if (!existsSync(distCli)) {
  fail('TypeScript SDK is not built. Run `cd libraries/typescript && npm run build` first.');
}

const work = mkdtempSync(join(tmpdir(), 'create-getpatter-smoke-'));
try {
  // Stage a locally-installed getpatter so the shim resolves it (no network).
  const gp = join(work, 'node_modules', 'getpatter');
  mkdirSync(gp, { recursive: true });
  copyFileSync(join(TS_SDK, 'package.json'), join(gp, 'package.json'));
  cpSync(join(TS_SDK, 'dist'), join(gp, 'dist'), { recursive: true });
  // The bundled CLI eagerly requires `express`; link the SDK's deps so the
  // module loads. A real npm install of getpatter ships these transitively.
  symlinkSync(join(TS_SDK, 'node_modules'), join(gp, 'node_modules'));

  // 1. --help forwards to the wizard.
  const help = spawnSync(process.execPath, [SHIM, '--help'], { cwd: work, encoding: 'utf8' });
  if (help.status !== 0) fail(`--help exited ${help.status}: ${help.stderr}`);
  if (!help.stdout.includes('Scaffold a new Patter voice-agent project')) {
    fail(`--help did not reach the wizard. Got:\n${help.stdout}`);
  }
  console.log('ok  --help forwards to `getpatter init`');

  // 2. Positional name + scaffold.
  const scaffold = spawnSync(
    process.execPath,
    [SHIM, 'my-app', '--yes', '--no-git', '--no-skills'],
    { cwd: work, encoding: 'utf8' },
  );
  if (scaffold.status !== 0) fail(`scaffold exited ${scaffold.status}: ${scaffold.stderr}`);

  const app = join(work, 'my-app');
  for (const rel of ['src/index.ts', 'package.json', '.env', '.env.example', '.gitignore', 'README.md']) {
    if (!existsSync(join(app, rel))) fail(`expected scaffold file missing: ${rel}`);
  }
  const pkg = JSON.parse(readFileSync(join(app, 'package.json'), 'utf8'));
  if (pkg.name !== 'my-app') fail(`positional name not applied: package.json name = ${pkg.name}`);

  const mode = statSync(join(app, '.env')).mode & 0o777;
  if (mode !== 0o600) fail(`.env mode is ${mode.toString(8)}, expected 600`);

  const index = readFileSync(join(app, 'src', 'index.ts'), 'utf8');
  if (!index.includes('from "getpatter"')) fail('src/index.ts does not import from getpatter');

  console.log('ok  `create-getpatter my-app` scaffolds a runnable project (.env @ 0600)');
  console.log('\nPASS: create-getpatter shim works end-to-end.');
} finally {
  rmSync(work, { recursive: true, force: true });
}
