#!/usr/bin/env node

/**
 * `create-getpatter` — the thin bootstrap behind `npm create getpatter`.
 *
 * `npm create getpatter my-app -- --mode pipeline` resolves to running this
 * bin as `create-getpatter my-app --mode pipeline`. All this shim does is hand
 * those arguments to the TypeScript SDK's setup wizard (`getpatter init`),
 * which owns every prompt, the provider matrix, and the scaffold codegen — see
 * `libraries/typescript/src/init/cli.ts` (behavioural mirror of the Python
 * `getpatter/init/cli.py`). Keeping the wizard logic in the SDK and re-using it
 * here means there is exactly one implementation to maintain, with full
 * Python <-> TypeScript parity.
 *
 * Argument handling:
 *   - The first bare positional (e.g. `my-app`) becomes `--name my-app`, the
 *     ergonomic shape `npm create <tool> <dir>` users expect. If the caller
 *     passes `--name` explicitly, the positional is still mapped but the
 *     explicit flag the wizard sees last wins — so prefer one or the other.
 *   - Every flag is forwarded verbatim (`--mode`, `--yes`, `--help`, ...).
 *
 * Resolution strategy (no bundled copy of the wizard — single source of truth):
 *   1. If `getpatter` is already resolvable on this machine (installed in the
 *      cwd's node_modules or globally), spawn its `init` directly — no network.
 *   2. Otherwise fall back to `npx -y getpatter@<pinned-version> init`, which
 *      npm fetches on demand. This is the same network access `npm create`
 *      already implies, so it costs nothing extra.
 *
 * This file is plain Node with zero dependencies so it runs the instant npm
 * fetches it, before anything is installed.
 */

import { spawn, spawnSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));

/** Pinned getpatter version this shim targets (kept in lockstep with the SDK). */
function pinnedVersion() {
  try {
    const pkg = JSON.parse(readFileSync(join(HERE, 'package.json'), 'utf8'));
    return typeof pkg.version === 'string' && pkg.version.length > 0 ? pkg.version : 'latest';
  } catch {
    return 'latest';
  }
}

/**
 * Turn the raw argv tail into the argument list `getpatter init` expects.
 * The first bare positional is promoted to `--name <value>`.
 */
function buildInitArgs(argv) {
  const out = [];
  let positionalConsumed = false;
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (!positionalConsumed && !a.startsWith('-')) {
      out.push('--name', a);
      positionalConsumed = true;
      continue;
    }
    out.push(a);
  }
  return out;
}

/**
 * Walk up from a path until a directory containing a `getpatter` package.json
 * is found, returning that directory. Used to locate the package root from its
 * resolved main entry (the `exports` map blocks resolving `package.json`
 * directly).
 */
function findPackageRoot(startFile) {
  let dir = dirname(startFile);
  for (let i = 0; i < 8; i++) {
    const candidate = join(dir, 'package.json');
    try {
      const pkg = JSON.parse(readFileSync(candidate, 'utf8'));
      if (pkg.name === 'getpatter') return { dir, pkg };
    } catch {
      /* keep walking */
    }
    const parent = dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return null;
}

/**
 * Try to locate an already-installed `getpatter` CLI entry point so we can run
 * it without touching the network. Returns the absolute path to the CLI JS
 * file, or null if getpatter isn't resolvable from here.
 */
function resolveLocalCli() {
  // Resolve relative to the directory `npm create` is run in, not this shim's
  // own (npx-cached) location — that's where a project's getpatter would live.
  const req = createRequire(join(process.cwd(), 'noop.js'));
  let mainEntry;
  try {
    mainEntry = req.resolve('getpatter');
  } catch {
    return null; // not installed locally — fall through to npx
  }
  const root = findPackageRoot(mainEntry);
  if (!root) return null;
  // `bin.getpatter` points at dist/cli.js — the CLI entry that owns `init`.
  const binField = root.pkg.bin;
  const binRel = typeof binField === 'string' ? binField : binField && binField.getpatter;
  if (!binRel) return null;
  return join(root.dir, binRel);
}

function main() {
  const argv = process.argv.slice(2);
  const initArgs = buildInitArgs(argv);

  const localCli = resolveLocalCli();
  if (localCli) {
    const res = spawnSync(process.execPath, [localCli, 'init', ...initArgs], {
      stdio: 'inherit',
    });
    process.exit(res.status ?? 1);
  }

  // Fall back to fetching the published package on demand via npx.
  const ref = `getpatter@${pinnedVersion()}`;
  const child = spawn('npx', ['-y', ref, 'init', ...initArgs], {
    stdio: 'inherit',
    shell: process.platform === 'win32',
  });
  child.on('error', (err) => {
    if (err && err.code === 'ENOENT') {
      console.error(
        'create-getpatter needs `npx` (Node.js) on your PATH to fetch the ' +
          'Patter CLI. Install Node 18+ from https://nodejs.org, then re-run ' +
          '`npm create getpatter`.',
      );
    } else {
      console.error(`create-getpatter failed to launch the Patter wizard: ${err.message}`);
    }
    process.exit(1);
  });
  child.on('exit', (code) => process.exit(code ?? 0));
}

main();
