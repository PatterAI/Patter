import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import {
  CallLogger,
  SCHEMA_VERSION,
  resolveLogRoot,
} from '../../src/services/call-log';

function mkTmp(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'patter-log-'));
}

function rmTree(dir: string): void {
  if (!fs.existsSync(dir)) return;
  fs.rmSync(dir, { recursive: true, force: true });
}

function findFile(root: string, suffix: string): string | null {
  const walk = (d: string): string | null => {
    for (const entry of fs.readdirSync(d)) {
      const full = path.join(d, entry);
      const stat = fs.statSync(full);
      if (stat.isDirectory()) {
        const found = walk(full);
        if (found) return found;
      } else if (full.endsWith(suffix)) {
        return full;
      }
    }
    return null;
  };
  return walk(root);
}

describe('resolveLogRoot', () => {
  const originalEnv = { ...process.env };

  afterEach(() => {
    process.env = { ...originalEnv };
  });

  it('returns null when env is unset', () => {
    delete process.env.PATTER_LOG_DIR;
    expect(resolveLogRoot()).toBeNull();
  });

  it('uses explicit value over env', () => {
    process.env.PATTER_LOG_DIR = '/should/be/ignored';
    expect(resolveLogRoot('/forced')).toBe('/forced');
  });

  it('resolves "auto" to platform default', () => {
    process.env.PATTER_LOG_DIR = 'auto';
    const resolved = resolveLogRoot();
    expect(resolved).not.toBeNull();
    expect(path.basename(resolved!)).toBe('patter');
  });

  it('honours env var when no explicit arg', () => {
    const tmp = mkTmp();
    try {
      process.env.PATTER_LOG_DIR = tmp;
      expect(resolveLogRoot()).toBe(tmp);
    } finally {
      rmTree(tmp);
    }
  });
});

describe('CallLogger (disabled)', () => {
  it('is a no-op and never writes files', async () => {
    const tmp = mkTmp();
    try {
      const logger = new CallLogger(null);
      expect(logger.enabled).toBe(false);
      await logger.logCallStart('c1', { caller: '+15551234567' });
      await logger.logTurn('c1', { role: 'user', text: 'hi' });
      await logger.logEvent('c1', 'tool_call', { name: 'lookup' });
      await logger.logCallEnd('c1', { durationSeconds: 10 });
      expect(fs.readdirSync(tmp)).toHaveLength(0);
    } finally {
      rmTree(tmp);
    }
  });
});

describe('CallLogger (enabled)', () => {
  let tmp: string;
  const originalEnv = { ...process.env };

  beforeEach(() => {
    tmp = mkTmp();
  });

  afterEach(() => {
    rmTree(tmp);
    process.env = { ...originalEnv };
  });

  it('writes metadata atomically on call start with default phone masking', async () => {
    process.env.PATTER_LOG_REDACT_PHONE = 'mask';
    const logger = new CallLogger(tmp);
    await logger.logCallStart('call-123', {
      caller: '+15551234567',
      callee: '+15557654321',
      telephonyProvider: 'twilio',
      providerMode: 'openai_realtime',
      agent: { provider: 'openai_realtime', voice: 'nova' },
    });
    const file = findFile(tmp, 'metadata.json')!;
    expect(file).toMatch(/calls\/\d{4}\/\d{2}\/\d{2}\/call-123\/metadata\.json$/);
    const parsed = JSON.parse(fs.readFileSync(file, 'utf8')) as Record<string, unknown>;
    expect(parsed.schema_version).toBe(SCHEMA_VERSION);
    expect(parsed.call_id).toBe('call-123');
    expect(parsed.status).toBe('in_progress');
    expect(parsed.telephony_provider).toBe('twilio');
    expect(parsed.caller).toMatch(/^\*\*\*.*4567$/);
  });

  it('honours full phone mode', async () => {
    process.env.PATTER_LOG_REDACT_PHONE = 'full';
    const logger = new CallLogger(tmp);
    await logger.logCallStart('c', { caller: '+15551234567' });
    const parsed = JSON.parse(
      fs.readFileSync(findFile(tmp, 'metadata.json')!, 'utf8'),
    ) as Record<string, unknown>;
    expect(parsed.caller).toBe('+15551234567');
  });

  it('hash_only mode produces sha256 hex prefix', async () => {
    process.env.PATTER_LOG_REDACT_PHONE = 'hash_only';
    const logger = new CallLogger(tmp);
    await logger.logCallStart('c', { caller: '+15551234567' });
    const parsed = JSON.parse(
      fs.readFileSync(findFile(tmp, 'metadata.json')!, 'utf8'),
    ) as Record<string, string>;
    expect(parsed.caller).toMatch(/^sha256:[0-9a-f]{16}$/);
  });

  it('appends JSONL turns with schema_version and ts', async () => {
    const logger = new CallLogger(tmp);
    await logger.logCallStart('c1', {});
    await logger.logTurn('c1', { role: 'user', text: 'hello', turn_index: 0 });
    await logger.logTurn('c1', { role: 'assistant', text: 'hi!', turn_index: 0 });
    const tPath = findFile(tmp, 'transcript.jsonl')!;
    const lines = fs.readFileSync(tPath, 'utf8').trim().split('\n');
    expect(lines).toHaveLength(2);
    const first = JSON.parse(lines[0]) as Record<string, unknown>;
    expect(first.schema_version).toBe(SCHEMA_VERSION);
    expect(first.text).toBe('hello');
    expect(typeof first.ts).toBe('string');
  });

  it('appends operational events to events.jsonl', async () => {
    const logger = new CallLogger(tmp);
    await logger.logCallStart('c1', {});
    await logger.logEvent('c1', 'barge_in', { offset_ms: 850 });
    const ePath = findFile(tmp, 'events.jsonl')!;
    const record = JSON.parse(fs.readFileSync(ePath, 'utf8').trim()) as Record<string, unknown>;
    expect(record.type).toBe('barge_in');
    expect((record.data as Record<string, unknown>).offset_ms).toBe(850);
  });

  it('finalises metadata on call end and preserves original fields', async () => {
    const logger = new CallLogger(tmp);
    await logger.logCallStart('c1', { caller: '+15551112222' });
    await logger.logCallEnd('c1', {
      durationSeconds: 42.5,
      turns: 3,
      cost: { total: 0.05, stt: 0.01 },
      latency: { p50_ms: 400, p95_ms: 900 },
    });
    const parsed = JSON.parse(
      fs.readFileSync(findFile(tmp, 'metadata.json')!, 'utf8'),
    ) as Record<string, unknown>;
    expect(parsed.status).toBe('completed');
    expect(parsed.duration_ms).toBe(42500);
    expect(parsed.turns).toBe(3);
    expect((parsed.cost as Record<string, number>).total).toBe(0.05);
    expect((parsed.latency as Record<string, number>).p95_ms).toBe(900);
    // Original caller redaction preserved.
    expect(parsed.call_id).toBe('c1');
    expect(String(parsed.caller)).toMatch(/2222$/);
  });

  it('write after root removal does not throw', async () => {
    const logger = new CallLogger(tmp);
    await logger.logCallStart('c1', {});
    rmTree(tmp);
    await expect(logger.logTurn('c1', { role: 'user', text: 'boom' })).resolves.not.toThrow();
    await expect(logger.logEvent('c1', 'error', { detail: 'sim' })).resolves.not.toThrow();
    await expect(logger.logCallEnd('c1', { durationSeconds: 1 })).resolves.not.toThrow();
  });

  it('logCallEnd without start still writes minimal envelope', async () => {
    const logger = new CallLogger(tmp);
    await logger.logCallEnd('orphan', { durationSeconds: 1, status: 'error', error: 'boom' });
    const parsed = JSON.parse(
      fs.readFileSync(findFile(tmp, 'metadata.json')!, 'utf8'),
    ) as Record<string, unknown>;
    expect(parsed.call_id).toBe('orphan');
    expect(parsed.status).toBe('error');
    expect(parsed.error).toBe('boom');
  });

  it('persists recording_path when local recording was active', async () => {
    const logger = new CallLogger(tmp);
    await logger.logCallStart('c1', {});
    await logger.logCallEnd('c1', {
      durationSeconds: 5,
      recordingPath: '/tmp/calls/c1/recording.wav',
    });
    const parsed = JSON.parse(
      fs.readFileSync(findFile(tmp, 'metadata.json')!, 'utf8'),
    ) as Record<string, unknown>;
    expect(parsed.recording_path).toBe('/tmp/calls/c1/recording.wav');
  });

  it('omits recording_path entirely when absent', async () => {
    const logger = new CallLogger(tmp);
    await logger.logCallStart('c1', {});
    await logger.logCallEnd('c1', { durationSeconds: 5 });
    const parsed = JSON.parse(
      fs.readFileSync(findFile(tmp, 'metadata.json')!, 'utf8'),
    ) as Record<string, unknown>;
    expect('recording_path' in parsed).toBe(false);
  });

  it('exposes callDir so call artifacts (recording WAV) land next to metadata.json', async () => {
    const logger = new CallLogger(tmp);
    await logger.logCallStart('c1', {});
    const dir = logger.callDir('c1');
    expect(dir).not.toBeNull();
    expect(fs.existsSync(path.join(dir!, 'metadata.json'))).toBe(true);
    // Disabled logger → null.
    expect(new CallLogger(null).callDir('c1')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Retention sweep
// ---------------------------------------------------------------------------

describe('CallLogger retention sweep', () => {
  let tmp: string;
  const originalEnv = { ...process.env };

  beforeEach(() => {
    tmp = mkTmp();
  });

  afterEach(() => {
    rmTree(tmp);
    process.env = { ...originalEnv };
  });

  function sweep(logger: CallLogger): Promise<void> {
    return (logger as unknown as { sweepOldDays(): Promise<void> }).sweepOldDays();
  }

  it('removes old day dirs INCLUDING local recordings', async () => {
    // The local recording WAV lives inside the per-call directory, so the
    // retention sweep that removes aged day-directories deletes recordings
    // too — no separate retention policy needed.
    process.env.PATTER_LOG_RETENTION_DAYS = '7';
    const logger = new CallLogger(tmp);
    const oldDir = path.join(tmp, 'calls', '2000', '01', '15', 'old-call');
    fs.mkdirSync(oldDir, { recursive: true });
    fs.writeFileSync(path.join(oldDir, 'metadata.json'), '{}');
    fs.writeFileSync(path.join(oldDir, 'transcript.jsonl'), '');
    const recording = path.join(oldDir, 'recording.wav');
    fs.writeFileSync(recording, Buffer.concat([Buffer.from('RIFF'), Buffer.alloc(40)]));
    await sweep(logger);
    expect(fs.existsSync(recording)).toBe(false);
    expect(fs.existsSync(oldDir)).toBe(false);
  });

  it('keeps everything when retention is 0', async () => {
    process.env.PATTER_LOG_RETENTION_DAYS = '0';
    const logger = new CallLogger(tmp);
    const oldDir = path.join(tmp, 'calls', '2000', '01', '15', 'old-call');
    fs.mkdirSync(oldDir, { recursive: true });
    fs.writeFileSync(path.join(oldDir, 'recording.wav'), 'RIFF');
    await sweep(logger);
    expect(fs.existsSync(path.join(oldDir, 'recording.wav'))).toBe(true);
  });
});
