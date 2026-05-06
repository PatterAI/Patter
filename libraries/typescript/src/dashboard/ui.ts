/**
 * Dashboard HTML — loaded from the bundled ``ui.html`` asset.
 *
 * The actual UI is a Vite + React SPA living at the repo root in
 * ``dashboard-app/``. ``npm run build && npm run sync`` from that directory
 * emits a single self-contained HTML file (JS + CSS inlined by
 * ``vite-plugin-singlefile``) and copies it next to this module as
 * ``src/dashboard/ui.html``. The TS build step then copies the same file
 * to ``dist/dashboard/ui.html`` so it ships inside the npm tarball.
 *
 * At runtime we resolve the asset relative to ``__dirname`` (CJS) or the
 * shimmed equivalent in ESM (``tsup.config.ts`` enables ``shims: true``).
 * Two candidate locations cover both layouts:
 *   1. ``<here>/ui.html``               — src dev (vitest reads off src/)
 *   2. ``<here>/dashboard/ui.html``     — bundled dist layout
 */

import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';

const FALLBACK_HTML = `<!doctype html>
<html><head><meta charset="utf-8"><title>Patter dashboard</title></head>
<body style="font-family:ui-sans-serif,system-ui;padding:2rem;color:#1a1a1a">
<h1>Dashboard asset missing</h1>
<p>The bundled <code>ui.html</code> was not found alongside this module.
Run <code>cd dashboard-app &amp;&amp; npm run build &amp;&amp; npm run sync</code>
from the repo root to regenerate it.</p>
</body></html>`;

function loadDashboardHtml(): string {
  // ``__dirname`` is shimmed by tsup in ESM (see ``tsup.config.ts``).
  // In CJS it's native; in vitest the module runs directly off ``src/`` so
  // the first candidate wins.
  const here = typeof __dirname !== 'undefined' ? __dirname : dirname('.');
  const candidates = [
    join(here, 'ui.html'),
    join(here, 'dashboard', 'ui.html'),
    join(here, '..', 'dashboard', 'ui.html'),
  ];
  for (const path of candidates) {
    try {
      return readFileSync(path, 'utf8');
    } catch {
      // try next candidate
    }
  }
  return FALLBACK_HTML;
}

/** Self-contained dashboard HTML+CSS+JS bundle served from `GET /`. */
export const DASHBOARD_HTML: string = loadDashboardHtml();
