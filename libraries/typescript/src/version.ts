/**
 * SDK version constant — auto-derived from ``package.json`` at runtime.
 *
 * tsup builds with ``shims: true`` so ``__dirname`` resolves to the
 * dist directory in both CJS and ESM. Reading ``../package.json``
 * from there always lands on the installed package's manifest. The
 * fallback covers the (unlikely) case where the file is missing.
 *
 * Source of truth: ``libraries/typescript/package.json#version``.
 */
import * as fs from 'node:fs';
import * as path from 'node:path';

function readVersion(): string {
  try {
    const pkgPath = path.resolve(__dirname, '..', 'package.json');
    const pkg = JSON.parse(fs.readFileSync(pkgPath, 'utf8')) as { version?: string };
    return typeof pkg.version === 'string' && pkg.version.length > 0 ? pkg.version : '';
  } catch {
    return '';
  }
}

export const VERSION: string = readVersion();
