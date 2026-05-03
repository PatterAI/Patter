/**
 * SDK version constant — kept in sync with ``package.json``.
 *
 * Hard-coded (rather than imported from ``package.json``) so the SDK works in
 * both bundled (no JSON loader) and ESM/CJS dual-export environments without
 * platform-specific JSON-import flags.
 */
export const VERSION = '0.5.5';
