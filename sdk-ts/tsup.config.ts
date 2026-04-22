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
  external: [
    // Optional tunnel adapters — loaded via ``await import(...)`` in
    // src/tunnel.ts. Must stay as runtime imports so the consumer's
    // installed package is used rather than a bundled copy.
    "cloudflared",
    "@ngrok/ngrok",
  ],
});
