/**
 * Unit tests for the `getpatter init` setup wizard (TypeScript port).
 *
 * These exercise the REAL scaffold builders and the real flag parser — no
 * mocking. The codegen is parity-critical: each generated file must match the
 * Python reference (`libraries/python/getpatter/init/cli.py`) byte-for-byte
 * (camelCase / `new` / options-object being the only permitted divergence).
 * The assertions pin the exact import lines, agent-call shapes, env-key
 * ordering, pip-extra resolution, and the security invariants.
 */

import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { VERSION } from '../src/version';
import {
  buildEnv,
  buildGitignore,
  buildIndexTs,
  buildMainPy,
  buildPackageJson,
  buildReadme,
  buildRequirementsTxt,
  InitError,
  npmPackageName,
  parseInitArgs,
  runInit,
  validateName,
  type ScaffoldPlan,
} from '../src/init/cli';

function plan(overrides: Partial<ScaffoldPlan> = {}): ScaffoldPlan {
  return {
    name: 'my-patter-agent',
    runtime: 'typescript',
    mode: 'realtime',
    engine: 'OpenAIRealtime2',
    stt: null,
    llm: null,
    tts: null,
    carrier: 'twilio',
    phone: '+15550001234',
    ...overrides,
  };
}

describe('[unit] init wizard — flag parsing', () => {
  it('parses value flags and toggles, ignoring unknown tokens', () => {
    const args = parseInitArgs([
      '--name',
      'foo',
      '--mode',
      'pipeline',
      '--stt',
      'cartesia',
      '--carrier',
      'telnyx',
      '--skip-keys',
      '--no-git',
      '--bogus',
      '-y',
    ]);
    expect(args.name).toBe('foo');
    expect(args.mode).toBe('pipeline');
    expect(args.stt).toBe('cartesia');
    expect(args.carrier).toBe('telnyx');
    expect(args.skipKeys).toBe(true);
    expect(args.noGit).toBe(true);
    expect(args.yes).toBe(true);
  });

  it('rejects invalid enum values (leaves them null)', () => {
    const args = parseInitArgs(['--mode', 'nonsense', '--runtime', 'rust']);
    expect(args.mode).toBeNull();
    expect(args.runtime).toBeNull();
  });
});

describe('[unit] init wizard — realtime codegen', () => {
  it('TS scaffold imports the default engine with camelCase + new', () => {
    const code = buildIndexTs(plan());
    expect(code).toContain('import { Patter, Twilio, OpenAIRealtime2 } from "getpatter";');
    expect(code).toContain('carrier: new Twilio(),');
    expect(code).toContain('engine: new OpenAIRealtime2(),');
    expect(code).toContain('await phone.serve({ agent, tunnel: true });');
    expect(code).not.toContain('stt:');
    expect(code).not.toContain('llm:');
  });

  it('python scaffold uses snake_case kwargs', () => {
    const code = buildMainPy(plan({ runtime: 'python' }));
    expect(code).toContain('from getpatter import Patter, Twilio, OpenAIRealtime2');
    expect(code).toContain('engine=OpenAIRealtime2(),');
    expect(code).toContain('await phone.serve(agent, tunnel=True)');
  });
});

describe('[unit] init wizard — pipeline codegen', () => {
  const pipe = plan({
    mode: 'pipeline',
    engine: null,
    stt: 'deepgram',
    llm: 'cerebras',
    tts: 'elevenlabs',
  });

  it('TS scaffold composes STT + LLM + TTS in order', () => {
    const code = buildIndexTs(pipe);
    expect(code).toContain(
      'import { Patter, Twilio, DeepgramSTT, CerebrasLLM, ElevenLabsTTS } from "getpatter";',
    );
    expect(code).toContain('stt: new DeepgramSTT(),');
    expect(code).toContain('llm: new CerebrasLLM(),');
    expect(code).toContain('tts: new ElevenLabsTTS(),');
    expect(code).not.toContain('engine:');
  });
});

describe('[unit] init wizard — env + manifest', () => {
  it('orders env keys carrier-first, then providers, then phone env', () => {
    const env = buildEnv(
      plan({
        mode: 'pipeline',
        engine: null,
        stt: 'assemblyai',
        llm: 'anthropic',
        tts: 'cartesia',
        carrier: 'telnyx',
      }),
    );
    const keys = env
      .split('\n')
      .filter((l) => l.includes('=') && !l.startsWith('#'))
      .map((l) => l.split('=')[0]);
    expect(keys).toEqual([
      'TELNYX_API_KEY',
      'TELNYX_CONNECTION_ID',
      'ASSEMBLYAI_API_KEY',
      'ANTHROPIC_API_KEY',
      'CARTESIA_API_KEY',
      'TELNYX_PHONE_NUMBER',
    ]);
  });

  it('keeps provided values and blanks the rest', () => {
    const env = buildEnv(plan(), { OPENAI_API_KEY: 'sekret' });
    expect(env).toContain('OPENAI_API_KEY=sekret');
    expect(env).toContain('TWILIO_ACCOUNT_SID=');
  });

  it('requirements.txt pins getpatter with sorted pip extras', () => {
    const req = buildRequirementsTxt(
      plan({
        mode: 'pipeline',
        engine: null,
        stt: 'assemblyai',
        llm: 'anthropic',
        tts: 'cartesia',
      }),
    );
    expect(req).toBe(`getpatter[anthropic,assemblyai,cartesia]==${VERSION}\n`);
  });

  it('requirements.txt pins the inworld extra when InworldTTS is chosen', () => {
    // InworldTTS imports aiohttp at runtime (pyproject `inworld` extra). A
    // python-runtime scaffold that selects it must pin getpatter[inworld] or
    // the agent ImportErrors at call construction.
    const req = buildRequirementsTxt(
      plan({
        runtime: 'python',
        mode: 'pipeline',
        engine: null,
        stt: 'deepgram',
        llm: 'openai',
        tts: 'inworld',
      }),
    );
    expect(req).toBe(`getpatter[inworld]==${VERSION}\n`);
  });

  it('package.json pins getpatter and wires the start script', () => {
    const pkgRaw = buildPackageJson(plan({ name: 'My Cool Agent' }));
    const pkg = JSON.parse(pkgRaw) as {
      name: string;
      scripts: Record<string, string>;
      dependencies: Record<string, string>;
      devDependencies: Record<string, string>;
    };
    expect(pkg.name).toBe('my-cool-agent');
    expect(pkg.scripts.start).toBe('tsx src/index.ts');
    expect(pkg.dependencies.getpatter).toBe(VERSION);
    expect(pkg.devDependencies.tsx).toBeDefined();
    expect(pkg.devDependencies.typescript).toBeDefined();
  });

  it('.gitignore always keeps .env out of version control', () => {
    const gi = buildGitignore(plan());
    expect(gi).toContain('.env');
    expect(gi).toContain('!.env.example');
    expect(gi).toContain('node_modules/');
  });
});

describe('[unit] init wizard — README tunnel prose is runtime-aware', () => {
  it('uses Python keyword-argument syntax for runtime=python', () => {
    const readme = buildReadme(plan({ runtime: 'python' }));
    expect(readme).toContain('(`tunnel=True`)');
    expect(readme).not.toContain('tunnel: true');
  });

  it('uses TS options-object syntax for runtime=typescript', () => {
    // The generated index.ts uses `tunnel: true`; the README prose must match
    // the TS runtime instead of leaking Python `tunnel=True` syntax.
    const readme = buildReadme(plan({ runtime: 'typescript' }));
    expect(readme).toContain('(`tunnel: true`)');
    expect(readme).not.toContain('tunnel=True');
  });
});

describe('[unit] init wizard — full scaffold to disk via runInit', () => {
  const dirs: string[] = [];
  afterEach(() => {
    for (const d of dirs.splice(0)) fs.rmSync(d, { recursive: true, force: true });
  });

  it('writes every file and chmods .env to 0600 (non-interactive)', async () => {
    const target = fs.mkdtempSync(path.join(os.tmpdir(), 'patter-init-'));
    dirs.push(target);
    // Drives the REAL orchestrator non-interactively (--yes path: no readline,
    // no skills, no git). Same code an end user runs.
    const code = await runInit([
      '--yes',
      '--name',
      target,
      '--force',
      '--skip-keys',
      '--no-skills',
      '--no-git',
    ]);
    expect(code).toBe(0);

    for (const f of ['src/index.ts', 'package.json', '.env', '.env.example', '.gitignore', 'README.md']) {
      expect(fs.existsSync(path.join(target, f))).toBe(true);
    }

    // SECURITY: .env must be owner-only (0600).
    const mode = fs.statSync(path.join(target, '.env')).mode & 0o777;
    expect(mode).toBe(0o600);
  });

  it('blocks scaffolding into a non-empty dir without --force', async () => {
    const target = fs.mkdtempSync(path.join(os.tmpdir(), 'patter-init-'));
    dirs.push(target);
    fs.writeFileSync(path.join(target, 'keep.txt'), 'keep me');
    const code = await runInit(['--yes', '--name', target, '--skip-keys']);
    expect(code).toBe(1);
    expect(fs.readFileSync(path.join(target, 'keep.txt'), 'utf8')).toBe('keep me');
    expect(fs.existsSync(path.join(target, 'src/index.ts'))).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Security regression tests — project-name sanitization (issue 3)
// ---------------------------------------------------------------------------

describe('[unit] init wizard — project-name validation & sanitization', () => {
  it('rejects a double-quote that would corrupt package.json', () => {
    expect(() => validateName('evil"proj')).toThrow(InitError);
  });

  it('rejects path traversal', () => {
    expect(() => validateName('../../etc/passwd')).toThrow(InitError);
  });

  it('rejects shell metacharacters and spaces', () => {
    for (const bad of ['foo;rm -rf', 'foo$(whoami)', 'foo`id`', 'foo bar']) {
      expect(() => validateName(bad)).toThrow(InitError);
    }
  });

  it('accepts a safe label and a path prefix with a safe final segment', () => {
    expect(() => validateName('my-agent')).not.toThrow();
    expect(() => validateName('/tmp/some/dir/my_agent.v2')).not.toThrow();
  });

  it('npmPackageName lowercases, dashes spaces, strips unsafe chars, truncates', () => {
    expect(npmPackageName('My Cool Agent')).toBe('my-cool-agent');
    expect(npmPackageName('evil"proj')).not.toContain('"');
    expect(npmPackageName('a'.repeat(300))).toBe('a'.repeat(214));
  });

  it('package.json is always valid JSON (name is JSON-encoded, never raw)', () => {
    const parsed = JSON.parse(buildPackageJson(plan({ name: 'my-cool-agent' }))) as {
      name: string;
      dependencies: Record<string, string>;
    };
    expect(parsed.name).toBe('my-cool-agent');
    expect(parsed.dependencies.getpatter).toBe(VERSION);
  });
});

describe('[unit] init wizard — invalid --name fails fast', () => {
  it('returns exit code 2 and writes nothing for an unsafe name', async () => {
    const base = fs.mkdtempSync(path.join(os.tmpdir(), 'patter-init-'));
    try {
      const target = path.join(base, 'bad"name');
      const code = await runInit(['--yes', '--name', target, '--skip-keys', '--no-git']);
      expect(code).toBe(2);
      expect(fs.existsSync(path.join(target, 'src/index.ts'))).toBe(false);
    } finally {
      fs.rmSync(base, { recursive: true, force: true });
    }
  });
});

// ---------------------------------------------------------------------------
// Security regression test — atomic 0600 .env (issue 2, no chmod race)
// ---------------------------------------------------------------------------

describe('[unit] init wizard — .env created 0600 atomically (no race)', () => {
  it('creates .env owner-only even under a permissive process umask', async () => {
    // The secure path uses openSync(p, "w", 0o600) + fchmodSync(fd, 0o600),
    // so the mode is set at creation — there is no window where .env is
    // world-/group-readable. We force a maximally permissive umask (0): if the
    // old write-then-chmod path were used, the file would still settle at 0600,
    // but the openSync-mode path guarantees it is never *created* world-wide.
    const prevUmask = process.umask(0o000);
    const target = fs.mkdtempSync(path.join(os.tmpdir(), 'patter-init-'));
    try {
      const code = await runInit([
        '--yes',
        '--name',
        target,
        '--force',
        '--skip-keys',
        '--no-skills',
        '--no-git',
      ]);
      expect(code).toBe(0);
      const mode = fs.statSync(path.join(target, '.env')).mode & 0o777;
      expect(mode).toBe(0o600);
    } finally {
      process.umask(prevUmask);
      fs.rmSync(target, { recursive: true, force: true });
    }
  });
});
