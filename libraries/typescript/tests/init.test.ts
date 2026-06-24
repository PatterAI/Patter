/**
 * End-to-end tests for the `getpatter init` setup wizard — exercises the REAL
 * `runInit` orchestrator writing a real scaffold into an `os.tmpdir()` dir.
 *
 * Parity target: `libraries/python/tests/test_init_wizard.py`. The assertions
 * mirror the Python suite — files exist, the generated `index.ts` wires the
 * chosen mode/engine/providers/carrier, `package.json` pins the SDK version,
 * `.env` is owner-only (0600), and `.gitignore` keeps `.env` out of git — but
 * cover EVERY voice mode (realtime + pipeline) and EVERY carrier
 * (twilio + telnyx + plivo).
 *
 * The ONLY thing mocked is the outer process boundary: `node:child_process`
 * (the `npx skills add ...` IDE-skills shell-out and `git init`). Everything
 * inward — flag parsing, plan resolution, codegen string builders, the
 * write-to-disk flow, env-key ordering, pip/npm manifest generation, and the
 * chmod 0600 on `.env` — runs as real code. This is the same path an end user
 * runs; nothing about the scaffold output is faked.
 */

import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// Mock ONLY the external subprocess boundary (skills `npx` + `git init`).
// Returning status 0 lets the wizard's "skills installed" path run without
// spawning a real `npx`/`git`. Everything else in init/cli.ts stays real.
const spawnSyncMock = vi.fn(() => ({ status: 0, error: undefined } as never));
vi.mock('node:child_process', () => ({
  spawnSync: (...args: unknown[]) => spawnSyncMock(...args),
}));

import { VERSION } from '../src/version';
import { runInit } from '../src/init/cli';

// Carriers under test: class name + the env vars each one reads.
const CARRIER_CASES = [
  {
    carrier: 'twilio',
    cls: 'Twilio',
    env: ['TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN'],
    phoneEnv: 'TWILIO_PHONE_NUMBER',
  },
  {
    carrier: 'telnyx',
    cls: 'Telnyx',
    env: ['TELNYX_API_KEY', 'TELNYX_CONNECTION_ID'],
    phoneEnv: 'TELNYX_PHONE_NUMBER',
  },
  {
    carrier: 'plivo',
    cls: 'Plivo',
    env: ['PLIVO_AUTH_ID', 'PLIVO_AUTH_TOKEN'],
    phoneEnv: 'PLIVO_PHONE_NUMBER',
  },
] as const;

const dirs: string[] = [];

function tmpTarget(): string {
  const d = fs.mkdtempSync(path.join(os.tmpdir(), 'patter-init-e2e-'));
  dirs.push(d);
  return d;
}

beforeEach(() => {
  spawnSyncMock.mockClear();
});

afterEach(() => {
  for (const d of dirs.splice(0)) fs.rmSync(d, { recursive: true, force: true });
});

/**
 * Drive the REAL wizard non-interactively into `target`. `--yes` skips
 * readline; `--ide` exercises the (mocked) skills shell-out. We deliberately
 * do NOT pass --no-skills so the mocked subprocess boundary is hit.
 */
async function scaffold(
  target: string,
  extra: readonly string[],
): Promise<number> {
  return runInit([
    '--yes',
    '--name',
    target,
    '--force',
    '--skip-keys',
    '--ide',
    'claude-code,cursor',
    ...extra,
  ]);
}

// Files every TS scaffold must produce (mirrors the Python file-existence set,
// with src/index.ts + package.json in place of main.py + requirements.txt).
const EXPECTED_FILES = [
  'src/index.ts',
  'package.json',
  '.env',
  '.env.example',
  '.gitignore',
  'README.md',
] as const;

function assertCommonScaffold(target: string): void {
  for (const f of EXPECTED_FILES) {
    expect(fs.existsSync(path.join(target, f)), `${f} should exist`).toBe(true);
  }

  // SECURITY: .env owner-only (0600).
  const mode = fs.statSync(path.join(target, '.env')).mode & 0o777;
  expect(mode).toBe(0o600);

  // .gitignore keeps .env (and its variants) out of version control, while
  // explicitly un-ignoring the committable example.
  const gi = fs.readFileSync(path.join(target, '.gitignore'), 'utf8');
  expect(gi).toContain('.env');
  expect(gi).toContain('!.env.example');
  expect(gi).toContain('node_modules/');

  // package.json pins getpatter to the current SDK version and wires start.
  const pkg = JSON.parse(
    fs.readFileSync(path.join(target, 'package.json'), 'utf8'),
  ) as {
    dependencies: Record<string, string>;
    scripts: Record<string, string>;
    devDependencies: Record<string, string>;
  };
  expect(pkg.dependencies.getpatter).toBe(VERSION);
  expect(pkg.scripts.start).toBe('tsx src/index.ts');
  expect(pkg.devDependencies.tsx).toBeDefined();
  expect(pkg.devDependencies.typescript).toBeDefined();
}

describe('[unit] init wizard e2e — realtime mode across all carriers', () => {
  for (const c of CARRIER_CASES) {
    it(`scaffolds a realtime ${c.carrier} project (default engine) to disk`, async () => {
      const target = tmpTarget();
      const code = await scaffold(target, ['--mode', 'realtime', '--carrier', c.carrier]);
      expect(code).toBe(0);

      assertCommonScaffold(target);

      const index = fs.readFileSync(path.join(target, 'src/index.ts'), 'utf8');
      // Default realtime engine is OpenAIRealtime2; carrier wired via `new`.
      expect(index).toContain(
        `import { Patter, ${c.cls}, OpenAIRealtime2 } from "getpatter";`,
      );
      expect(index).toContain(`carrier: new ${c.cls}(),`);
      expect(index).toContain('engine: new OpenAIRealtime2(),');
      expect(index).toContain('phoneNumber: PHONE_NUMBER,');
      expect(index).toContain('await phone.serve({ agent, tunnel: true });');
      expect(index).toContain(`process.env.${c.phoneEnv}`);
      // Realtime must NOT emit pipeline wiring.
      expect(index).not.toContain('stt:');
      expect(index).not.toContain('llm:');
      expect(index).not.toContain('tts:');

      // .env lists the carrier creds first, then engine key, then phone env.
      const env = fs.readFileSync(path.join(target, '.env'), 'utf8');
      const keys = env
        .split('\n')
        .filter((l) => l.includes('=') && !l.startsWith('#'))
        .map((l) => l.split('=')[0]);
      expect(keys).toEqual([...c.env, 'OPENAI_API_KEY', c.phoneEnv]);
    });
  }
});

describe('[unit] init wizard e2e — pipeline mode across all carriers', () => {
  for (const c of CARRIER_CASES) {
    it(`scaffolds a pipeline ${c.carrier} project (default STT/LLM/TTS) to disk`, async () => {
      const target = tmpTarget();
      const code = await scaffold(target, ['--mode', 'pipeline', '--carrier', c.carrier]);
      expect(code).toBe(0);

      assertCommonScaffold(target);

      const index = fs.readFileSync(path.join(target, 'src/index.ts'), 'utf8');
      // Pipeline defaults: Deepgram STT, Cerebras LLM, ElevenLabs TTS.
      expect(index).toContain(
        `import { Patter, ${c.cls}, DeepgramSTT, CerebrasLLM, ElevenLabsTTS } from "getpatter";`,
      );
      expect(index).toContain(`carrier: new ${c.cls}(),`);
      expect(index).toContain('stt: new DeepgramSTT(),');
      expect(index).toContain('llm: new CerebrasLLM(),');
      expect(index).toContain('tts: new ElevenLabsTTS(),');
      expect(index).toContain('await phone.serve({ agent, tunnel: true });');
      // Pipeline must NOT emit a realtime engine.
      expect(index).not.toContain('engine:');

      // .env: carrier creds, then STT/LLM/TTS keys in order, then phone env.
      // (Deepgram + ElevenLabs + Cerebras; OpenAI absent for the defaults.)
      const env = fs.readFileSync(path.join(target, '.env'), 'utf8');
      const keys = env
        .split('\n')
        .filter((l) => l.includes('=') && !l.startsWith('#'))
        .map((l) => l.split('=')[0]);
      expect(keys).toEqual([
        ...c.env,
        'DEEPGRAM_API_KEY',
        'CEREBRAS_API_KEY',
        'ELEVENLABS_API_KEY',
        c.phoneEnv,
      ]);
    });
  }
});

describe('[unit] init wizard e2e — external boundary (skills) is mocked', () => {
  it('shells out to the mocked skills subprocess once per requested IDE', async () => {
    const target = tmpTarget();
    const code = await scaffold(target, ['--mode', 'realtime', '--carrier', 'twilio']);
    expect(code).toBe(0);

    // Two IDEs requested (claude-code, cursor) → two `npx skills add` calls,
    // each routed through the mock (no real npx/network).
    expect(spawnSyncMock).toHaveBeenCalledTimes(2);
    const [firstCmd, firstArgs] = spawnSyncMock.mock.calls[0] as [string, string[]];
    expect(firstCmd).toBe('npx');
    expect(firstArgs).toContain('skills');
    expect(firstArgs).toContain('add');
    expect(firstArgs).toContain('-a');
    // Pinned to the current SDK version: patterai/skills#v<version>.
    expect(firstArgs.some((a) => a === `patterai/skills#v${VERSION}`)).toBe(true);
    const ides = (spawnSyncMock.mock.calls as Array<[string, string[]]>).map(
      ([, a]) => a[a.length - 1],
    );
    expect(ides).toEqual(['claude-code', 'cursor']);
  });

  it('does not shell out when --no-skills is passed', async () => {
    const target = tmpTarget();
    const code = await runInit([
      '--yes',
      '--name',
      target,
      '--force',
      '--skip-keys',
      '--no-skills',
      '--mode',
      'realtime',
      '--carrier',
      'twilio',
    ]);
    expect(code).toBe(0);
    expect(spawnSyncMock).not.toHaveBeenCalled();
  });
});

describe('[unit] init wizard e2e — non-empty dir guard', () => {
  it('blocks scaffolding into a non-empty dir without --force', async () => {
    const target = tmpTarget();
    fs.writeFileSync(path.join(target, 'keep.txt'), 'keep me');
    const code = await runInit(['--yes', '--name', target, '--skip-keys', '--no-skills']);
    expect(code).toBe(1);
    // Original file untouched, no scaffold written.
    expect(fs.readFileSync(path.join(target, 'keep.txt'), 'utf8')).toBe('keep me');
    expect(fs.existsSync(path.join(target, 'src/index.ts'))).toBe(false);
    // Guard fires before any subprocess work.
    expect(spawnSyncMock).not.toHaveBeenCalled();
  });
});
