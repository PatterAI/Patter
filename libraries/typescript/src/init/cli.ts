/**
 * `getpatter init` — greenfield setup wizard (TypeScript port).
 *
 * Behavioural mirror of the Python reference implementation at
 * `libraries/python/getpatter/init/cli.py`. Keep the prompt strings, flag
 * names, default values, provider matrix, and scaffold codegen rules in sync
 * with that file — the only permitted divergence is language idiom
 * (snake_case → camelCase, `Patter(...)` kwargs → single options object,
 * `node:readline/promises` instead of stdlib `input()`).
 *
 * Invoked two ways, both routing here:
 *   * `getpatter init [options]`  — argv switch in `src/cli.ts`
 *   * `npm create getpatter`      — the `create-getpatter` bin re-dispatches
 *
 * Security: API keys are optional, written to `.env` with mode 0600, listed
 * in `.gitignore`, and NEVER echoed to stdout or logged.
 */

import { spawnSync } from 'node:child_process';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import * as readline from 'node:readline/promises';
import { VERSION } from '../version';

// Placeholder phone number — NANP 555 fiction range (see security rules).
// Never a real number. Used when the user supplies none via --phone / prompt.
const DEFAULT_PHONE = '+15550001234';

// Valid IDE targets for the skills shell-out (`npx skills add ... -a <ide>`).
const IDE_CHOICES = ['claude-code', 'cursor', 'copilot', 'codex'] as const;
type IdeChoice = (typeof IDE_CHOICES)[number];

// ---------------------------------------------------------------------------
// Provider matrix — the single source of truth the scaffold builders read.
// Each entry mirrors the Python dict: class (importable alias from getpatter),
// label (menu text), env (env var names), extra (pip extra — TS bundles all,
// kept only for parity / requirements.txt generation when runtime=python).
// ---------------------------------------------------------------------------

interface ProviderEntry {
  readonly cls: string;
  readonly label: string;
  readonly env: readonly string[];
  readonly extra: string;
}

interface CarrierEntry {
  readonly cls: string;
  readonly label: string;
  readonly env: readonly string[];
  readonly phoneEnv: string;
}

// Realtime engines (mode=realtime — pick ONE). Order = menu order.
const REALTIME_ENGINES: Record<string, ProviderEntry> = {
  OpenAIRealtime2: {
    cls: 'OpenAIRealtime2',
    label: 'OpenAI Realtime 2 (gpt-realtime-2) — default',
    env: ['OPENAI_API_KEY'],
    extra: '',
  },
  OpenAIRealtime: {
    cls: 'OpenAIRealtime',
    label: 'OpenAI Realtime (legacy gpt-realtime-mini)',
    env: ['OPENAI_API_KEY'],
    extra: '',
  },
  ElevenLabsConvAI: {
    cls: 'ElevenLabsConvAI',
    label: 'ElevenLabs ConvAI (needs ELEVENLABS_AGENT_ID)',
    env: ['ELEVENLABS_API_KEY', 'ELEVENLABS_AGENT_ID'],
    extra: '',
  },
};
const DEFAULT_REALTIME_ENGINE = 'OpenAIRealtime2';

// Pipeline STT providers.
const PIPELINE_STT: Record<string, ProviderEntry> = {
  deepgram: {
    cls: 'DeepgramSTT',
    label: 'Deepgram — default',
    env: ['DEEPGRAM_API_KEY'],
    extra: '',
  },
  assemblyai: {
    cls: 'AssemblyAISTT',
    label: 'AssemblyAI',
    env: ['ASSEMBLYAI_API_KEY'],
    extra: 'assemblyai',
  },
  cartesia: {
    cls: 'CartesiaSTT',
    label: 'Cartesia',
    env: ['CARTESIA_API_KEY'],
    extra: 'cartesia',
  },
  soniox: {
    cls: 'SonioxSTT',
    label: 'Soniox',
    env: ['SONIOX_API_KEY'],
    extra: 'soniox',
  },
  speechmatics: {
    cls: 'SpeechmaticsSTT',
    label: 'Speechmatics',
    env: ['SPEECHMATICS_API_KEY'],
    extra: 'speechmatics',
  },
  whisper: {
    cls: 'WhisperSTT',
    label: 'OpenAI Whisper',
    env: ['OPENAI_API_KEY'],
    extra: '',
  },
  openai: {
    cls: 'OpenAITranscribeSTT',
    label: 'OpenAI Transcribe',
    env: ['OPENAI_API_KEY'],
    extra: '',
  },
  fish_audio: {
    cls: 'FishAudioSTT',
    label: 'Fish Audio (batch — higher latency)',
    env: ['FISH_AUDIO_API_KEY'],
    extra: 'fish_audio',
  },
  xai: {
    cls: 'XaiSTT',
    label: 'xAI Grok (streaming)',
    env: ['XAI_API_KEY'],
    extra: 'xai',
  },
  telnyx: {
    cls: 'TelnyxSTT',
    label: 'Telnyx (on-network — telnyx/google/deepgram/azure)',
    env: ['TELNYX_API_KEY'],
    extra: 'telnyx-ai',
  },
};
const DEFAULT_STT = 'deepgram';

// Pipeline LLM providers.
const PIPELINE_LLM: Record<string, ProviderEntry> = {
  cerebras: {
    cls: 'CerebrasLLM',
    label: 'Cerebras (gpt-oss-120b) — default',
    env: ['CEREBRAS_API_KEY'],
    extra: 'cerebras',
  },
  openai: {
    cls: 'OpenAILLM',
    label: 'OpenAI',
    env: ['OPENAI_API_KEY'],
    extra: '',
  },
  anthropic: {
    cls: 'AnthropicLLM',
    label: 'Anthropic Claude',
    env: ['ANTHROPIC_API_KEY'],
    extra: 'anthropic',
  },
  groq: {
    cls: 'GroqLLM',
    label: 'Groq',
    env: ['GROQ_API_KEY'],
    extra: 'groq',
  },
  google: {
    cls: 'GoogleLLM',
    label: 'Google Gemini',
    env: ['GOOGLE_API_KEY'],
    extra: 'google',
  },
};
const DEFAULT_LLM = 'cerebras';

// Pipeline TTS providers.
const PIPELINE_TTS: Record<string, ProviderEntry> = {
  elevenlabs: {
    cls: 'ElevenLabsTTS',
    label: 'ElevenLabs — default',
    env: ['ELEVENLABS_API_KEY'],
    extra: '',
  },
  openai: {
    cls: 'OpenAITTS',
    label: 'OpenAI',
    env: ['OPENAI_API_KEY'],
    extra: '',
  },
  cartesia: {
    cls: 'CartesiaTTS',
    label: 'Cartesia',
    env: ['CARTESIA_API_KEY'],
    extra: 'cartesia',
  },
  rime: {
    cls: 'RimeTTS',
    label: 'Rime',
    env: ['RIME_API_KEY'],
    extra: 'rime',
  },
  lmnt: {
    cls: 'LMNTTTS',
    label: 'LMNT',
    env: ['LMNT_API_KEY'],
    extra: 'lmnt',
  },
  inworld: {
    cls: 'InworldTTS',
    label: 'Inworld',
    env: ['INWORLD_API_KEY'],
    extra: 'inworld',
  },
  fish_audio: {
    cls: 'FishAudioTTS',
    label: 'Fish Audio (s2.1-pro)',
    env: ['FISH_AUDIO_API_KEY'],
    extra: 'fish_audio',
  },
  xai: {
    cls: 'XaiTTS',
    label: 'xAI Grok (eve default)',
    env: ['XAI_API_KEY'],
    extra: 'xai',
  },
  telnyx: {
    cls: 'TelnyxTTS',
    label: 'Telnyx (NaturalHD — on-network)',
    env: ['TELNYX_API_KEY'],
    extra: 'telnyx-ai',
  },
};
const DEFAULT_TTS = 'elevenlabs';

// Telephony carriers.
const CARRIERS: Record<string, CarrierEntry> = {
  twilio: {
    cls: 'Twilio',
    label: 'Twilio — default',
    env: ['TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN'],
    phoneEnv: 'TWILIO_PHONE_NUMBER',
  },
  telnyx: {
    cls: 'Telnyx',
    label: 'Telnyx',
    env: ['TELNYX_API_KEY', 'TELNYX_CONNECTION_ID'],
    phoneEnv: 'TELNYX_PHONE_NUMBER',
  },
  plivo: {
    cls: 'Plivo',
    label: 'Plivo',
    env: ['PLIVO_AUTH_ID', 'PLIVO_AUTH_TOKEN'],
    phoneEnv: 'PLIVO_PHONE_NUMBER',
  },
};
const DEFAULT_CARRIER = 'twilio';

// ---------------------------------------------------------------------------
// Flag parsing — mirrors the Python argparse surface (same flag names).
// ---------------------------------------------------------------------------

export interface InitArgs {
  name: string | null;
  runtime: 'python' | 'typescript' | null;
  mode: 'realtime' | 'pipeline' | null;
  engine: string | null;
  stt: string | null;
  llm: string | null;
  tts: string | null;
  carrier: string | null;
  phone: string | null;
  skipKeys: boolean;
  ide: string | null;
  noSkills: boolean;
  noGit: boolean;
  force: boolean;
  yes: boolean;
  help: boolean;
}

const INIT_USAGE = `Usage: getpatter init [options]

Scaffold a new Patter voice-agent project (setup wizard).

Options:
  --name <dir>           Project name → target directory
  --runtime <r>          SDK runtime: python | typescript (default: typescript)
  --mode <m>             Voice mode: realtime | pipeline (default: realtime)
  --engine <e>           Realtime engine (realtime mode only)
  --stt <p>              STT provider (pipeline mode only)
  --llm <p>              LLM provider (pipeline mode only)
  --tts <p>              TTS provider (pipeline mode only)
  --carrier <c>          Telephony carrier: twilio | telnyx | plivo
  --phone <e164>         Phone number in E.164 (placeholder ${DEFAULT_PHONE})
  --skip-keys            Do not prompt for API keys; write placeholders to .env
  --ide <list>           Comma-separated IDE skills targets (${IDE_CHOICES.join(', ')})
  --no-skills            Skip the IDE skills install step
  --no-git               Skip 'git init' in the new project
  --force                Allow scaffolding into a non-empty directory
  --yes, -y              Non-interactive: accept all defaults`;

/**
 * Parse the `init` flags out of an argv slice (everything after `init`).
 * Unknown flags are tolerated (ignored) to stay forgiving like argparse's
 * usage here; value flags consume the following token.
 */
export function parseInitArgs(rest: readonly string[]): InitArgs {
  const args: InitArgs = {
    name: null,
    runtime: null,
    mode: null,
    engine: null,
    stt: null,
    llm: null,
    tts: null,
    carrier: null,
    phone: null,
    skipKeys: false,
    ide: null,
    noSkills: false,
    noGit: false,
    force: false,
    yes: false,
    help: false,
  };

  const takeValue = (i: number): string | null =>
    i + 1 < rest.length ? rest[i + 1] : null;

  for (let i = 0; i < rest.length; i++) {
    const a = rest[i];
    switch (a) {
      case '--name':
        args.name = takeValue(i);
        i++;
        break;
      case '--runtime': {
        const v = takeValue(i);
        if (v === 'python' || v === 'typescript') args.runtime = v;
        i++;
        break;
      }
      case '--mode': {
        const v = takeValue(i);
        if (v === 'realtime' || v === 'pipeline') args.mode = v;
        i++;
        break;
      }
      case '--engine':
        args.engine = takeValue(i);
        i++;
        break;
      case '--stt':
        args.stt = takeValue(i);
        i++;
        break;
      case '--llm':
        args.llm = takeValue(i);
        i++;
        break;
      case '--tts':
        args.tts = takeValue(i);
        i++;
        break;
      case '--carrier':
        args.carrier = takeValue(i);
        i++;
        break;
      case '--phone':
        args.phone = takeValue(i);
        i++;
        break;
      case '--ide':
        args.ide = takeValue(i);
        i++;
        break;
      case '--skip-keys':
        args.skipKeys = true;
        break;
      case '--no-skills':
        args.noSkills = true;
        break;
      case '--no-git':
        args.noGit = true;
        break;
      case '--force':
        args.force = true;
        break;
      case '--yes':
      case '-y':
        args.yes = true;
        break;
      case '--help':
      case '-h':
        args.help = true;
        break;
      default:
        // Ignore unknown tokens (forgiving, like the Python surface).
        break;
    }
  }
  return args;
}

// ---------------------------------------------------------------------------
// Selection plan — what the wizard resolved to.
// ---------------------------------------------------------------------------

export interface ScaffoldPlan {
  name: string;
  runtime: 'python' | 'typescript';
  mode: 'realtime' | 'pipeline';
  engine: string | null;
  stt: string | null;
  llm: string | null;
  tts: string | null;
  carrier: string;
  phone: string;
}

// ---------------------------------------------------------------------------
// Validation — guard the project name before any path resolution or codegen
// ---------------------------------------------------------------------------

/** User-facing error for invalid wizard input (bad project name, …). */
export class InitError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'InitError';
  }
}

// A project label (final path segment) may only contain these characters.
// Forbids quotes, shell metacharacters, and path separators that could break
// package.json or escape the target directory.
const SAFE_LABEL_RE = /^[A-Za-z0-9._-]+$/;

/**
 * Reject project names unsafe to use as a path or in generated files.
 *
 * Mirrors Python `_validate_name`: guards against path traversal (`..`),
 * path-separator injection in the label, and characters outside the safe set
 * (notably double-quotes that would corrupt package.json). The full name may
 * still be a path; only the final segment becomes the project label.
 */
export function validateName(name: string): void {
  if (!name || !name.trim()) {
    throw new InitError('Project name must not be empty.');
  }
  const parts = name.split(/[\\/]/);
  if (parts.some((p) => p === '..')) {
    throw new InitError(`Project name '${name}' must not contain '..' path segments.`);
  }
  const label = path.basename(name);
  if (!label) {
    throw new InitError(`Project name '${name}' has no final path segment.`);
  }
  if (!SAFE_LABEL_RE.test(label)) {
    throw new InitError(
      `Project name '${label}' contains invalid characters. ` +
        "Use only letters, digits, '.', '_' and '-'.",
    );
  }
}

/**
 * Coerce a project label into a valid npm package name: only `[a-z0-9._-]`,
 * capped at 214 chars. The label has already passed `validateName`.
 */
export function npmPackageName(label: string): string {
  const lowered = label.toLowerCase().replace(/ /g, '-');
  const cleaned = lowered.replace(/[^a-z0-9._-]/g, '') || 'patter-agent';
  return cleaned.slice(0, 214);
}

// ---------------------------------------------------------------------------
// Prompt helpers (node:readline/promises only — no third-party prompt libs)
// ---------------------------------------------------------------------------

async function askText(
  rl: readline.Interface,
  prompt: string,
  def: string,
): Promise<string> {
  const raw = (await rl.question(`${prompt} [${def}]: `)).trim();
  return raw || def;
}

/**
 * Prompt for a secret WITHOUT echoing the typed characters to the terminal.
 *
 * The default readline interface echoes every keystroke, so an API key typed
 * at the prompt is visible to a shoulder-surf or a terminal recording. We mute
 * the echo by temporarily overriding the interface's internal `_writeToOutput`
 * so only the prompt string is shown — the typed value never reaches stdout.
 * Restores the original writer afterwards so later prompts echo normally.
 */
async function askSecret(rl: readline.Interface, prompt: string): Promise<string> {
  // `_writeToOutput` is the internal hook readline uses to render the prompt
  // and echo input. Casting to access it is the stdlib-only equivalent of
  // Python's getpass — no third-party dependency.
  const iface = rl as unknown as {
    _writeToOutput?: (s: string) => void;
    output?: NodeJS.WritableStream;
  };
  const original = iface._writeToOutput?.bind(rl);
  let promptShown = false;
  iface._writeToOutput = (s: string): void => {
    // Show the prompt itself exactly once; swallow every echoed keystroke.
    if (!promptShown && s.includes(prompt)) {
      promptShown = true;
      iface.output?.write(prompt);
    }
    // Anything else (the echoed secret characters) is intentionally dropped.
  };
  try {
    const raw = (await rl.question(prompt)).trim();
    return raw;
  } finally {
    // Terminate the muted prompt line and restore normal echo.
    iface.output?.write('\n');
    if (original) iface._writeToOutput = original;
    else delete iface._writeToOutput;
  }
}

async function askChoice(
  rl: readline.Interface,
  prompt: string,
  options: ReadonlyArray<readonly [string, string]>,
  defaultKey: string,
): Promise<string> {
  const keys = options.map(([k]) => k);
  const defaultIdx = keys.indexOf(defaultKey) + 1;
  console.log(prompt);
  options.forEach(([key, label], i) => {
    const marker = key === defaultKey ? ' (default)' : '';
    console.log(`  ${i + 1}) ${label}${marker}`);
  });
  for (;;) {
    const raw = (
      await rl.question(`Choose [1-${options.length}, default ${defaultIdx}]: `)
    ).trim();
    if (!raw) return defaultKey;
    if (keys.includes(raw)) return raw;
    const n = Number.parseInt(raw, 10);
    if (/^\d+$/.test(raw) && n >= 1 && n <= options.length) return keys[n - 1];
    console.log(
      `  Invalid choice '${raw}' — enter a number 1-${options.length} ` +
        `or a name (${keys.join(', ')}).`,
    );
  }
}

async function askYesNo(
  rl: readline.Interface,
  prompt: string,
  def: boolean,
): Promise<boolean> {
  const hint = def ? 'Y/n' : 'y/N';
  const raw = (await rl.question(`${prompt} [${hint}]: `)).trim().toLowerCase();
  if (!raw) return def;
  return raw === 'y' || raw === 'yes';
}

async function askMulti(
  rl: readline.Interface,
  prompt: string,
  options: ReadonlyArray<readonly [string, string]>,
): Promise<string[]> {
  const keys = options.map(([k]) => k);
  console.log(prompt);
  options.forEach(([, label], i) => console.log(`  ${i + 1}) ${label}`));
  const raw = (await rl.question('Select (comma-separated, blank for none): ')).trim();
  if (!raw) return [];
  const chosen: string[] = [];
  for (const tokRaw of raw.split(',')) {
    const tok = tokRaw.trim();
    if (!tok) continue;
    if (keys.includes(tok) && !chosen.includes(tok)) {
      chosen.push(tok);
    } else if (/^\d+$/.test(tok)) {
      const n = Number.parseInt(tok, 10);
      if (n >= 1 && n <= options.length) {
        const k = keys[n - 1];
        if (!chosen.includes(k)) chosen.push(k);
      }
    }
  }
  return chosen;
}

// ---------------------------------------------------------------------------
// Selection resolution — flags + prompts → a concrete scaffold plan.
// ---------------------------------------------------------------------------

async function resolvePlan(
  rl: readline.Interface | null,
  args: InitArgs,
): Promise<ScaffoldPlan> {
  const interactive = rl !== null;

  // 1. Project name → target dir
  let name: string;
  if (args.name !== null) name = args.name;
  else if (interactive) name = await askText(rl!, 'Project name', 'my-patter-agent');
  else name = 'my-patter-agent';

  // Validate before anything is written or any path is resolved — a bad name
  // (path traversal, quote injection) must fail fast with a clear message.
  validateName(name);

  // 2. Runtime (this is the TypeScript CLI → prefill typescript)
  let runtime: 'python' | 'typescript';
  if (args.runtime !== null) {
    runtime = args.runtime;
  } else if (interactive) {
    runtime = (await askChoice(
      rl!,
      'SDK runtime?',
      [
        ['typescript', 'TypeScript (getpatter)'],
        ['python', 'Python (getpatter)'],
      ],
      'typescript',
    )) as 'python' | 'typescript';
  } else {
    runtime = 'typescript';
  }

  // 3. Voice mode
  let mode: 'realtime' | 'pipeline';
  if (args.mode !== null) {
    mode = args.mode;
  } else if (interactive) {
    mode = (await askChoice(
      rl!,
      'Voice mode?',
      [
        ['realtime', 'Realtime — one engine owns the turn (lowest latency)'],
        ['pipeline', 'Pipeline — compose STT + LLM + TTS (full control)'],
      ],
      'realtime',
    )) as 'realtime' | 'pipeline';
  } else {
    mode = 'realtime';
  }

  // 4. Engine / providers (filtered by mode)
  let engine: string | null = null;
  let stt: string | null = null;
  let llm: string | null = null;
  let tts: string | null = null;
  if (mode === 'realtime') {
    if (args.engine !== null && args.engine in REALTIME_ENGINES) engine = args.engine;
    else if (interactive) {
      engine = await askChoice(
        rl!,
        'Realtime engine?',
        Object.entries(REALTIME_ENGINES).map(([k, v]) => [k, v.label] as const),
        DEFAULT_REALTIME_ENGINE,
      );
    } else engine = DEFAULT_REALTIME_ENGINE;
  } else {
    if (args.stt !== null && args.stt in PIPELINE_STT) stt = args.stt;
    else if (interactive)
      stt = await askChoice(
        rl!,
        'STT provider?',
        Object.entries(PIPELINE_STT).map(([k, v]) => [k, v.label] as const),
        DEFAULT_STT,
      );
    else stt = DEFAULT_STT;

    if (args.llm !== null && args.llm in PIPELINE_LLM) llm = args.llm;
    else if (interactive)
      llm = await askChoice(
        rl!,
        'LLM provider?',
        Object.entries(PIPELINE_LLM).map(([k, v]) => [k, v.label] as const),
        DEFAULT_LLM,
      );
    else llm = DEFAULT_LLM;

    if (args.tts !== null && args.tts in PIPELINE_TTS) tts = args.tts;
    else if (interactive)
      tts = await askChoice(
        rl!,
        'TTS provider?',
        Object.entries(PIPELINE_TTS).map(([k, v]) => [k, v.label] as const),
        DEFAULT_TTS,
      );
    else tts = DEFAULT_TTS;
  }

  // 5. Carrier
  let carrier: string;
  if (args.carrier !== null && args.carrier in CARRIERS) carrier = args.carrier;
  else if (interactive) {
    carrier = await askChoice(
      rl!,
      'Telephony carrier?',
      Object.entries(CARRIERS).map(([k, v]) => [k, v.label] as const),
      DEFAULT_CARRIER,
    );
  } else carrier = DEFAULT_CARRIER;

  // Phone number
  let phone: string;
  if (args.phone !== null) phone = args.phone;
  else if (interactive) phone = await askText(rl!, 'Phone number (E.164)', DEFAULT_PHONE);
  else phone = DEFAULT_PHONE;

  return { name, runtime, mode, engine, stt, llm, tts, carrier, phone };
}

function collectEnvKeys(plan: ScaffoldPlan): string[] {
  const keys: string[] = [];
  const add = (names: readonly string[]): void => {
    for (const n of names) if (!keys.includes(n)) keys.push(n);
  };
  add(CARRIERS[plan.carrier].env);
  if (plan.mode === 'realtime') {
    add(REALTIME_ENGINES[plan.engine!].env);
  } else {
    add(PIPELINE_STT[plan.stt!].env);
    add(PIPELINE_LLM[plan.llm!].env);
    add(PIPELINE_TTS[plan.tts!].env);
  }
  add([CARRIERS[plan.carrier].phoneEnv]);
  return keys;
}

function collectExtras(plan: ScaffoldPlan): string[] {
  const extras = new Set<string>();
  if (plan.mode === 'pipeline') {
    const tables: ReadonlyArray<[Record<string, ProviderEntry>, string]> = [
      [PIPELINE_STT, plan.stt!],
      [PIPELINE_LLM, plan.llm!],
      [PIPELINE_TTS, plan.tts!],
    ];
    for (const [table, key] of tables) {
      const extra = table[key].extra;
      if (extra) extras.add(extra);
    }
  } else {
    const extra = REALTIME_ENGINES[plan.engine!].extra;
    if (extra) extras.add(extra);
  }
  return [...extras].sort();
}

// ---------------------------------------------------------------------------
// Scaffold string builders — pure functions, parity-critical codegen.
// ---------------------------------------------------------------------------

const PROMPT_LINES_PY =
  '            "You are the receptionist for Acme Corp. Help callers with "\n' +
  '            "hours, support questions, and simple account changes. "\n' +
  '            "Keep replies short."\n';

const PROMPT_LINES_TS =
  '      "You are the receptionist for Acme Corp. Help callers with " +\n' +
  '      "hours, support questions, and simple account changes. " +\n' +
  '      "Keep replies short.",\n';

export function buildMainPy(plan: ScaffoldPlan): string {
  const carrierCls = CARRIERS[plan.carrier].cls;
  const phoneEnv = CARRIERS[plan.carrier].phoneEnv;
  let imports: string;
  let agentBlock: string;
  if (plan.mode === 'realtime') {
    const engineCls = REALTIME_ENGINES[plan.engine!].cls;
    imports = `from getpatter import Patter, ${carrierCls}, ${engineCls}`;
    agentBlock =
      '    agent = phone.agent(\n' +
      `        engine=${engineCls}(),\n` +
      '        system_prompt=(\n' +
      PROMPT_LINES_PY +
      '        ),\n' +
      '        first_message="Hi! Thanks for calling Acme Corp. How can I help?",\n' +
      '    )';
  } else {
    const sttCls = PIPELINE_STT[plan.stt!].cls;
    const llmCls = PIPELINE_LLM[plan.llm!].cls;
    const ttsCls = PIPELINE_TTS[plan.tts!].cls;
    imports = `from getpatter import Patter, ${carrierCls}, ${sttCls}, ${llmCls}, ${ttsCls}`;
    agentBlock =
      '    agent = phone.agent(\n' +
      `        stt=${sttCls}(),\n` +
      `        llm=${llmCls}(),\n` +
      `        tts=${ttsCls}(),\n` +
      '        system_prompt=(\n' +
      PROMPT_LINES_PY +
      '        ),\n' +
      '        first_message="Hi! Thanks for calling Acme Corp. How can I help?",\n' +
      '    )';
  }
  return (
    '"""Patter voice agent — generated by `getpatter init`.\n' +
    '\n' +
    'Answers inbound calls and talks to the caller. Set the API keys in\n' +
    '.env (already scaffolded), then run:  python main.py\n' +
    '"""\n' +
    '\n' +
    'import asyncio\n' +
    'import os\n' +
    '\n' +
    `${imports}\n` +
    '\n' +
    `PHONE_NUMBER = os.environ.get("${phoneEnv}", "${plan.phone}")\n` +
    '\n' +
    '\n' +
    'async def main() -> None:\n' +
    '    phone = Patter(\n' +
    `        carrier=${carrierCls}(),   # reads creds from env\n` +
    '        phone_number=PHONE_NUMBER,\n' +
    '    )\n' +
    '\n' +
    `${agentBlock}\n` +
    '\n' +
    '    print(f"Ready on {PHONE_NUMBER}. Waiting for calls... (Ctrl+C to stop)")\n' +
    '    await phone.serve(agent, tunnel=True)\n' +
    '\n' +
    '\n' +
    'if __name__ == "__main__":\n' +
    '    asyncio.run(main())\n'
  );
}

export function buildIndexTs(plan: ScaffoldPlan): string {
  const carrierCls = CARRIERS[plan.carrier].cls;
  const phoneEnv = CARRIERS[plan.carrier].phoneEnv;
  let imports: string;
  let agentBlock: string;
  if (plan.mode === 'realtime') {
    const engineCls = REALTIME_ENGINES[plan.engine!].cls;
    imports = `import { Patter, ${carrierCls}, ${engineCls} } from "getpatter";`;
    agentBlock =
      '  const agent = phone.agent({\n' +
      `    engine: new ${engineCls}(),\n` +
      '    systemPrompt:\n' +
      PROMPT_LINES_TS +
      '    firstMessage: "Hi! Thanks for calling Acme Corp. How can I help?",\n' +
      '  });';
  } else {
    const sttCls = PIPELINE_STT[plan.stt!].cls;
    const llmCls = PIPELINE_LLM[plan.llm!].cls;
    const ttsCls = PIPELINE_TTS[plan.tts!].cls;
    imports = `import { Patter, ${carrierCls}, ${sttCls}, ${llmCls}, ${ttsCls} } from "getpatter";`;
    agentBlock =
      '  const agent = phone.agent({\n' +
      `    stt: new ${sttCls}(),\n` +
      `    llm: new ${llmCls}(),\n` +
      `    tts: new ${ttsCls}(),\n` +
      '    systemPrompt:\n' +
      PROMPT_LINES_TS +
      '    firstMessage: "Hi! Thanks for calling Acme Corp. How can I help?",\n' +
      '  });';
  }
  return (
    '/**\n' +
    ' * Patter voice agent — generated by `getpatter init`.\n' +
    ' *\n' +
    ' * Answers inbound calls and talks to the caller. Set the API keys in\n' +
    ' * .env (already scaffolded), then run:  npm start\n' +
    ' */\n' +
    `${imports}\n` +
    '\n' +
    `const PHONE_NUMBER = process.env.${phoneEnv} ?? "${plan.phone}";\n` +
    '\n' +
    'async function main(): Promise<void> {\n' +
    '  const phone = new Patter({\n' +
    `    carrier: new ${carrierCls}(),   // reads creds from env\n` +
    '    phoneNumber: PHONE_NUMBER,\n' +
    '  });\n' +
    '\n' +
    `${agentBlock}\n` +
    '\n' +
    '  console.log(`Ready on ${PHONE_NUMBER}. Waiting for calls... (Ctrl+C to stop)`);\n' +
    '  await phone.serve({ agent, tunnel: true });\n' +
    '}\n' +
    '\n' +
    'main().catch((err) => {\n' +
    '  console.error(err);\n' +
    '  process.exit(1);\n' +
    '});\n'
  );
}

export function buildRequirementsTxt(plan: ScaffoldPlan): string {
  const extras = collectExtras(plan);
  let spec = 'getpatter';
  if (extras.length) spec += `[${extras.join(',')}]`;
  spec += `==${VERSION}`;
  return `${spec}\n`;
}

export function buildEnv(
  plan: ScaffoldPlan,
  withValues: Record<string, string> = {},
): string {
  const lines = [
    '# Patter environment — generated by `getpatter init`.',
    '# Fill in the blanks below. Keep this file out of version control.',
    '',
  ];
  for (const key of collectEnvKeys(plan)) {
    lines.push(`${key}=${withValues[key] ?? ''}`);
  }
  return `${lines.join('\n')}\n`;
}

export function buildEnvExample(plan: ScaffoldPlan): string {
  return buildEnv(plan, {});
}

export function buildGitignore(plan: ScaffoldPlan): string {
  const common = ['.env', '.env.*', '!.env.example'];
  const rest =
    plan.runtime === 'python'
      ? ['__pycache__/', '*.pyc', '.venv/', 'venv/', '.pytest_cache/']
      : ['node_modules/', 'dist/', '*.log'];
  return `${[...common, ...rest].join('\n')}\n`;
}

function projectLabel(plan: ScaffoldPlan): string {
  return path.basename(plan.name) || 'patter-agent';
}

export function buildPackageJson(plan: ScaffoldPlan): string {
  // Sanitize to a valid npm name, then JSON-encode so a stray quote / unicode
  // in the label can never produce malformed JSON that breaks `npm install`.
  const name = JSON.stringify(npmPackageName(projectLabel(plan)));
  return (
    '{\n' +
    `  "name": ${name},\n` +
    '  "version": "0.1.0",\n' +
    '  "private": true,\n' +
    '  "type": "module",\n' +
    '  "scripts": {\n' +
    '    "start": "tsx src/index.ts"\n' +
    '  },\n' +
    '  "dependencies": {\n' +
    `    "getpatter": "${VERSION}"\n` +
    '  },\n' +
    '  "devDependencies": {\n' +
    '    "tsx": "^4.0.0",\n' +
    '    "typescript": "^5.0.0"\n' +
    '  }\n' +
    '}\n'
  );
}

export function buildReadme(plan: ScaffoldPlan): string {
  let runBlock: string;
  let entry: string;
  if (plan.runtime === 'python') {
    runBlock =
      '```sh\n' +
      'pip install -r requirements.txt\n' +
      '# edit .env with your API keys\n' +
      'python main.py\n' +
      '```';
    entry = 'main.py';
  } else {
    runBlock = '```sh\nnpm install\n# edit .env with your API keys\nnpm start\n```';
    entry = 'src/index.ts';
  }
  let stack: string;
  if (plan.mode === 'realtime') {
    stack = `Realtime engine: \`${REALTIME_ENGINES[plan.engine!].cls}\``;
  } else {
    stack =
      `Pipeline: STT \`${PIPELINE_STT[plan.stt!].cls}\` · ` +
      `LLM \`${PIPELINE_LLM[plan.llm!].cls}\` · ` +
      `TTS \`${PIPELINE_TTS[plan.tts!].cls}\``;
  }
  const tunnelRef = plan.runtime === 'python' ? '`tunnel=True`' : '`tunnel: true`';
  return (
    `# ${projectLabel(plan)}\n` +
    '\n' +
    'A Patter voice agent, scaffolded by `getpatter init`.\n' +
    '\n' +
    `- Carrier: \`${CARRIERS[plan.carrier].cls}\`\n` +
    `- ${stack}\n` +
    `- Entry point: \`${entry}\`\n` +
    '\n' +
    '## Run\n' +
    '\n' +
    `${runBlock}\n` +
    '\n' +
    'The agent answers inbound calls and starts a Cloudflare Quick Tunnel\n' +
    `(${tunnelRef}) so the carrier can reach your local server during dev.\n`
  );
}

// ---------------------------------------------------------------------------
// Filesystem + side effects
// ---------------------------------------------------------------------------

function writeScaffold(
  target: string,
  plan: ScaffoldPlan,
  envValues: Record<string, string>,
): string[] {
  fs.mkdirSync(target, { recursive: true });
  const written: string[] = [];

  const w = (rel: string, content: string, mode?: number): void => {
    const p = path.join(target, rel);
    fs.mkdirSync(path.dirname(p), { recursive: true });
    if (mode !== undefined) {
      // Create the file with the target mode atomically, so a secrets-bearing
      // file (.env) is NEVER world-/group-readable for the window between
      // write and a separate chmod (TOCTOU). The mode passed to openSync is
      // masked by umask on create, so fchmodSync re-asserts it; using the fd
      // (not the path) closes the race even on a re-scaffold (--force).
      const fd = fs.openSync(p, 'w', mode);
      try {
        fs.fchmodSync(fd, mode);
        fs.writeSync(fd, content, null, 'utf8');
      } finally {
        fs.closeSync(fd);
      }
    } else {
      fs.writeFileSync(p, content, { encoding: 'utf8' });
    }
    written.push(p);
  };

  if (plan.runtime === 'python') {
    w('main.py', buildMainPy(plan));
    w('requirements.txt', buildRequirementsTxt(plan));
  } else {
    w('src/index.ts', buildIndexTs(plan));
    w('package.json', buildPackageJson(plan));
  }

  // .env first (0600), then the committable example.
  w('.env', buildEnv(plan, envValues), 0o600);
  w('.env.example', buildEnvExample(plan));
  w('.gitignore', buildGitignore(plan));
  w('README.md', buildReadme(plan));
  return written;
}

function maybeGitInit(target: string): void {
  const res = spawnSync('git', ['init', '--quiet'], {
    cwd: target,
    stdio: 'ignore',
  });
  if (res.error || res.status !== 0) {
    console.log("  'git init' failed or git not found — skipping.");
    return;
  }
  console.log('  Initialised a git repository.');
}

function maybeInstallSkills(target: string, ides: readonly string[]): void {
  if (!ides.length) return;
  const ref = `patterai/skills#v${VERSION}`;
  for (const ide of ides) {
    console.log(`  Installing skills for ${ide}...`);
    const res = spawnSync('npx', ['skills', 'add', ref, '-a', ide], {
      cwd: target,
      stdio: 'inherit',
    });
    if (res.error || res.status !== 0) {
      console.log(`  skills add for ${ide} failed (or npx missing) — skipping.`);
    }
  }
}

// ---------------------------------------------------------------------------
// Orchestration
// ---------------------------------------------------------------------------

/**
 * Run the wizard. Returns a process exit code.
 *
 * @param rest argv slice after the `init` token.
 */
export async function runInit(rest: readonly string[]): Promise<number> {
  const args = parseInitArgs(rest);
  if (args.help) {
    console.log(INIT_USAGE);
    return 0;
  }

  const interactive = !args.yes;
  const rl = interactive
    ? readline.createInterface({ input: process.stdin, output: process.stdout })
    : null;

  try {
    const plan = await resolvePlan(rl, args);

    const target = path.resolve(expandHome(plan.name));

    // 9. Non-empty dir guard (--force overrides).
    if (fs.existsSync(target) && fs.readdirSync(target).length > 0 && !args.force) {
      console.log(
        `Target directory ${target} is not empty. ` +
          'Re-run with --force to scaffold into it anyway.',
      );
      return 1;
    }

    const envKeys = collectEnvKeys(plan);

    // 6. API keys → .env (0600). Skippable; secrets never echoed/logged.
    const envValues: Record<string, string> = {};
    if (!args.skipKeys && interactive && rl) {
      console.log(
        '\nAPI keys (input is hidden — press Enter to leave blank, fill them in later):',
      );
      for (const key of envKeys) {
        // askSecret suppresses echo so the raw key never lands on a shared
        // terminal or a recording. Written to .env (0600) only — never logged.
        const val = await askSecret(rl, `  ${key}: `);
        if (val) envValues[key] = val;
      }
    }
    // In --skip-keys (or non-interactive) mode every value stays a placeholder.

    const written = writeScaffold(target, plan, envValues);

    // 7. IDE skills (multi-select). --no-skills / non-interactive default skips.
    let ides: string[] = [];
    if (!args.noSkills) {
      if (args.ide !== null) {
        ides = args.ide
          .split(',')
          .map((s) => s.trim())
          .filter((s): s is IdeChoice => (IDE_CHOICES as readonly string[]).includes(s));
      } else if (interactive && rl) {
        ides = await askMulti(
          rl,
          'Install Patter skills for which IDEs?',
          IDE_CHOICES.map((k) => [k, k] as const),
        );
      }
    }
    maybeInstallSkills(target, ides);

    // 8. git init (optional). --no-git / non-interactive default skips.
    let doGit = false;
    if (!args.noGit) {
      doGit = interactive && rl ? await askYesNo(rl, 'Initialise a git repository?', true) : false;
    }
    if (doGit) maybeGitInit(target);

    // Summary — paths only, never secret values.
    console.log(`\nScaffolded ${plan.runtime} project at ${target}`);
    for (const p of written) console.log(`  + ${path.relative(target, p)}`);
    if (plan.runtime === 'python') {
      console.log(
        `\nNext:  cd ${target}  &&  pip install -r requirements.txt  &&  python main.py`,
      );
    } else {
      console.log(`\nNext:  cd ${target}  &&  npm install  &&  npm start`);
    }
    return 0;
  } catch (err) {
    if (err instanceof InitError) {
      console.error(`error: ${err.message}`);
      return 2;
    }
    throw err;
  } finally {
    if (rl) rl.close();
  }
}

function expandHome(p: string): string {
  if (p === '~' || p.startsWith('~/')) {
    return path.join(os.homedir(), p.slice(1));
  }
  return p;
}
