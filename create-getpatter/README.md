# create-getpatter

The npm `create` shim that powers:

```sh
npm create getpatter my-app
# or, forwarding flags:
npm create getpatter my-app -- --mode pipeline --carrier telnyx --yes
```

It scaffolds a runnable Patter voice-agent project — exactly what
`getpatter init` produces.

## What it is

A ~120-line, **zero-dependency** Node bootstrap (`index.js`). It does not
contain any wizard logic of its own. All prompts, the provider matrix, and the
scaffold codegen live in the SDK's setup wizard
(`libraries/typescript/src/init/cli.ts`, the behavioural mirror of the Python
`getpatter/init/cli.py`). The shim's only job is to hand its arguments to
`getpatter init`.

## Design choice — why a re-dispatch, not a bundled copy

Two options were on the table:

1. **Spawn the published `getpatter init`** (what this does).
2. **Bundle / re-export a minimal copy of the wizard** inside this package.

Option 1 wins on the principle that matters most here — **single source of
truth**. The wizard is non-trivial (provider matrix, parity-locked codegen,
secure `.env` writing). Duplicating it into a second package guarantees drift:
the day someone adds a provider to the SDK, `create-getpatter` would silently
scaffold the old set. By re-dispatching, the shim is always in lockstep with
whatever `getpatter` version it targets, and there is exactly one
implementation to test and maintain.

### How "spawn" avoids a needless network hit

`create-getpatter` resolves the CLI in two steps:

1. **Local first (no network):** if `getpatter` is already resolvable from the
   directory where `npm create` runs (installed in the project's
   `node_modules` or globally), it spawns that package's `getpatter init`
   directly. Nothing is fetched.
2. **`npx` fallback:** otherwise it runs
   `npx -y getpatter@<pinned-version> init`, letting npm fetch the package on
   demand. This is the same network access `npm create` already implies, so it
   adds no new dependency at create-time — the package itself has an empty
   dependency tree.

The targeted `getpatter` version is pinned in this package's `version` field
(kept in lockstep with the SDK — currently `0.6.3`).

## Argument handling

- The first bare positional (`my-app`) is promoted to `--name my-app`, the
  ergonomic `npm create <tool> <dir>` shape.
- Every flag is forwarded verbatim to `getpatter init`: `--mode`, `--runtime`,
  `--engine`, `--stt`, `--llm`, `--tts`, `--carrier`, `--phone`, `--skip-keys`,
  `--ide`, `--no-skills`, `--no-git`, `--force`, `--yes`/`-y`, `--help`/`-h`.

Run `npm create getpatter -- --help` to see the full wizard usage.

## Verifying locally

```sh
# Build the SDK so a local getpatter CLI exists, then run the smoke test:
cd libraries/typescript && npm run build && cd ../..
node create-getpatter/index.js --help        # forwards to the wizard
npm --prefix create-getpatter test            # full end-to-end smoke test
```

`smoke-test.mjs` links the freshly-built SDK as a local `getpatter`, exercises
the no-network resolution path, and asserts that
`create-getpatter my-app --yes` scaffolds a project whose `.env` is written at
mode `0600`.
