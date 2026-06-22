import { defineConfig } from "tsup";

/**
 * tsup build config for the Patter TypeScript SDK.
 *
 * The entry point is ``src/index.ts`` (dual CJS + ESM). The CLI build
 * (``src/cli.ts``, CJS-only) runs as a second invocation from
 * ``package.json`` scripts — tsup CLI flags there override `entry` for
 * that pass.
 *
 * External packages (NOT bundled into dist): these are dynamic imports
 * resolved at runtime from the consumer's ``node_modules``. Bundling
 * them breaks when they are CJS and try to call ``require()`` from
 * inside the ESM dist (e.g. cloudflared uses ``require("path")``).
 */
export default defineConfig({
  entry: ["src/index.ts"],
  format: ["cjs", "esm"],
  dts: true,
  // Shim ``__dirname`` / ``__filename`` in the ESM bundle so the dashboard
  // module can locate the bundled ``ui.html`` asset at runtime via
  // ``readFileSync(__dirname + '/dashboard/ui.html')`` regardless of
  // module format.
  shims: true,
  external: [
    // Optional tunnel adapters — loaded via ``await import(...)`` in
    // src/tunnel.ts. Must stay as runtime imports so the consumer's
    // installed package is used rather than a bundled copy.
    "cloudflared",
    "@ngrok/ngrok",
    // Optional YAML suite loader — `await import("yaml")` in src/evals/runner.ts.
    // Kept external so JSON-only users don't need the dependency installed.
    "yaml",
  ],
});
