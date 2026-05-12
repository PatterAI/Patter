import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vitest/config';

// Mirror the build-time SDK version constant from vite.config.ts so any
// test that ends up importing App.tsx (which references __SDK_VERSION__)
// resolves the symbol against the same source of truth.
const here = fileURLToPath(new URL('.', import.meta.url));
const sdkPkgPath = resolve(here, '..', 'libraries', 'typescript', 'package.json');
const sdkPkg = JSON.parse(readFileSync(sdkPkgPath, 'utf8')) as { version: string };

// Vitest config separate from vite.config.ts so the SPA build (singlefile
// inline) is unaffected by the test runner. Only ``test`` files are picked
// up; the bundle still ships from src/ via vite.config.ts.
export default defineConfig({
  define: {
    __SDK_VERSION__: JSON.stringify(sdkPkg.version),
  },
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx'],
  },
});
