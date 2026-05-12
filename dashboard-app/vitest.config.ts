import { defineConfig } from 'vitest/config';

// Vitest config separate from vite.config.ts so the SPA build (singlefile
// inline) is unaffected by the test runner. Only ``test`` files are picked
// up; the bundle still ships from src/ via vite.config.ts.
export default defineConfig({
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx'],
  },
});
