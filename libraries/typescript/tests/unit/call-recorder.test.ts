/**
 * Unit tests for src/audio/call-recorder.ts — LocalCallRecorder.
 *
 * Feeds scripted PCM / mulaw frames through a recorder instance and parses
 * the resulting WAV bytes directly (RIFF header fields + interleaved
 * samples), asserting stereo layout, sample rate, per-channel sample counts,
 * silence-padding alignment, buffered-write behaviour, and finalization on
 * abnormal teardown. Mirror of Python tests/unit/test_call_recorder.py.
 */
import { describe, it, expect, afterEach } from 'vitest';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import {
  AGENT_BACKLOG_CAP_S,
  LocalCallRecorder,
  RECORDING_SAMPLE_RATE,
} from '../../src/audio/call-recorder';
import { mulawToPcm16 } from '../../src/audio/transcoding';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const FRAME_SAMPLES_20MS = RECORDING_SAMPLE_RATE / 50; // 320 samples

const tmpDirs: string[] = [];

function mkTmp(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'patter-rec-'));
  tmpDirs.push(dir);
  return dir;
}

afterEach(() => {
  while (tmpDirs.length > 0) {
    fs.rmSync(tmpDirs.pop()!, { recursive: true, force: true });
  }
});

/** Constant-amplitude PCM16 LE mono frame. */
function pcmFrame(value: number, samples = FRAME_SAMPLES_20MS): Buffer {
  const buf = Buffer.alloc(samples * 2);
  for (let i = 0; i < samples; i++) buf.writeInt16LE(value, i * 2);
  return buf;
}

/** 20 ms of constant mulaw 8 kHz (160 bytes). */
function mulawFrame(byte = 0x55, numBytes = 160): Buffer {
  return Buffer.alloc(numBytes, byte);
}

interface ParsedWav {
  readonly riffSize: number;
  readonly audioFormat: number;
  readonly channels: number;
  readonly sampleRate: number;
  readonly byteRate: number;
  readonly blockAlign: number;
  readonly bitsPerSample: number;
  readonly dataSize: number;
  readonly frames: number;
  readonly left: number[];
  readonly right: number[];
}

/** Parse the recorder's canonical 44-byte-header WAV file. */
function readWav(p: string): ParsedWav {
  const buf = fs.readFileSync(p);
  expect(buf.subarray(0, 4).toString('ascii')).toBe('RIFF');
  expect(buf.subarray(8, 12).toString('ascii')).toBe('WAVE');
  expect(buf.subarray(12, 16).toString('ascii')).toBe('fmt ');
  expect(buf.subarray(36, 40).toString('ascii')).toBe('data');
  const channels = buf.readUInt16LE(22);
  const dataSize = buf.readUInt32LE(40);
  expect(44 + dataSize).toBe(buf.length); // header sizes match real bytes
  const left: number[] = [];
  const right: number[] = [];
  for (let off = 44; off + 4 <= 44 + dataSize; off += 4) {
    left.push(buf.readInt16LE(off));
    right.push(buf.readInt16LE(off + 2));
  }
  return {
    riffSize: buf.readUInt32LE(4),
    audioFormat: buf.readUInt16LE(20),
    channels,
    sampleRate: buf.readUInt32LE(24),
    byteRate: buf.readUInt32LE(28),
    blockAlign: buf.readUInt16LE(32),
    bitsPerSample: buf.readUInt16LE(34),
    dataSize,
    frames: dataSize / 4,
    left,
    right,
  };
}

function uniq(values: number[]): number[] {
  return [...new Set(values)].sort((a, b) => a - b);
}

// ---------------------------------------------------------------------------
// Format + counts
// ---------------------------------------------------------------------------

describe('LocalCallRecorder — WAV format', () => {
  it('writes a canonical stereo 16 kHz PCM16 header', () => {
    const dir = mkTmp();
    const rec = new LocalCallRecorder(path.join(dir, 'recording.wav'));
    for (let i = 0; i < 10; i++) rec.addCallerAudio(pcmFrame(1000)); // 200 ms
    const returned = rec.close();

    expect(returned).toBe(path.join(dir, 'recording.wav'));
    const wav = readWav(returned!);
    expect(wav.channels).toBe(2);
    expect(wav.sampleRate).toBe(RECORDING_SAMPLE_RATE);
    expect(wav.bitsPerSample).toBe(16);
    expect(wav.audioFormat).toBe(1); // PCM
    expect(wav.blockAlign).toBe(4);
    expect(wav.byteRate).toBe(RECORDING_SAMPLE_RATE * 4);
    expect(wav.frames).toBe(10 * FRAME_SAMPLES_20MS);
    expect(wav.riffSize).toBe(36 + wav.dataSize);
  });

  it('creates parent directories', () => {
    const dir = mkTmp();
    const nested = path.join(dir, 'a', 'b', 'recording.wav');
    const rec = new LocalCallRecorder(nested);
    rec.addCallerAudio(pcmFrame(7));
    rec.close();
    expect(fs.existsSync(nested)).toBe(true);
  });

  it('maps left=caller, right=agent', () => {
    const dir = mkTmp();
    const rec = new LocalCallRecorder(path.join(dir, 'r.wav'));
    // Agent first so its FIFO is primed, then one caller frame clocks the
    // pair out together.
    rec.addAgentAudio(pcmFrame(-2000));
    rec.addCallerAudio(pcmFrame(1000));
    rec.close();

    const wav = readWav(path.join(dir, 'r.wav'));
    expect(uniq(wav.left)).toEqual([1000]);
    expect(uniq(wav.right)).toEqual([-2000]);
  });
});

// ---------------------------------------------------------------------------
// Alignment + silence padding
// ---------------------------------------------------------------------------

describe('LocalCallRecorder — alignment', () => {
  it('pads the right channel with silence when the agent is idle', () => {
    const dir = mkTmp();
    const rec = new LocalCallRecorder(path.join(dir, 'r.wav'));
    for (let i = 0; i < 50; i++) rec.addCallerAudio(pcmFrame(123)); // 1 s
    rec.close();

    const wav = readWav(path.join(dir, 'r.wav'));
    expect(wav.frames).toBe(RECORDING_SAMPLE_RATE); // 1 s
    expect(uniq(wav.left)).toEqual([123]);
    expect(uniq(wav.right)).toEqual([0]);
  });

  it('drains a TTS burst at the caller clock, then silence', () => {
    const dir = mkTmp();
    const rec = new LocalCallRecorder(path.join(dir, 'r.wav'));
    // Agent burst arrives up-front (TTS pushes faster than realtime).
    for (let i = 0; i < 25; i++) rec.addAgentAudio(pcmFrame(-7)); // 0.5 s
    for (let i = 0; i < 50; i++) rec.addCallerAudio(pcmFrame(9)); // 1 s clock
    rec.close();

    const wav = readWav(path.join(dir, 'r.wav'));
    expect(wav.frames).toBe(RECORDING_SAMPLE_RATE);
    expect(uniq(wav.left)).toEqual([9]);
    const half = RECORDING_SAMPLE_RATE / 2;
    expect(uniq(wav.right.slice(0, half))).toEqual([-7]);
    expect(uniq(wav.right.slice(half))).toEqual([0]);
    expect(rec.callerSamples).toBe(RECORDING_SAMPLE_RATE);
    expect(rec.agentSamples).toBe(half);
  });

  it('commits agent-only audio on close, padded with left silence', () => {
    const dir = mkTmp();
    const rec = new LocalCallRecorder(path.join(dir, 'r.wav'));
    for (let i = 0; i < 10; i++) rec.addAgentAudio(pcmFrame(42));
    expect(rec.framesWritten).toBe(0); // nothing clocked out yet
    rec.close();

    const wav = readWav(path.join(dir, 'r.wav'));
    expect(wav.frames).toBe(10 * FRAME_SAMPLES_20MS);
    expect(uniq(wav.left)).toEqual([0]);
    expect(uniq(wav.right)).toEqual([42]);
  });

  it('keeps channels separate through an interleaved conversation', () => {
    const dir = mkTmp();
    const rec = new LocalCallRecorder(path.join(dir, 'r.wav'));
    for (let i = 0; i < 20; i++) {
      if (i % 2 === 0) rec.addAgentAudio(pcmFrame(-1));
      rec.addCallerAudio(pcmFrame(1));
    }
    rec.close();

    const wav = readWav(path.join(dir, 'r.wav'));
    expect(uniq(wav.left)).toEqual([1]);
    expect(uniq(wav.right)).toEqual([-1, 0]); // agent audio + padding, never mixed
  });
});

// ---------------------------------------------------------------------------
// Encodings (mulaw 8k / pcm16 8k / pcm16 24k → PCM16 16 kHz)
// ---------------------------------------------------------------------------

describe('LocalCallRecorder — encodings', () => {
  it('decodes + upsamples mulaw 8k on both channels', () => {
    const dir = mkTmp();
    const rec = new LocalCallRecorder(path.join(dir, 'r.wav'));
    for (let i = 0; i < 50; i++) {
      rec.addAgentAudio(mulawFrame(0x10), 'mulaw_8k');
      rec.addCallerAudio(mulawFrame(0x55), 'mulaw_8k');
    }
    rec.close();

    const wav = readWav(path.join(dir, 'r.wav'));
    // 50 × 160 mulaw bytes @8k → ×2 upsample ≈ 16000 samples (1 s); the
    // stateful resampler may hold back a couple of samples of filter state.
    expect(Math.abs(wav.frames - RECORDING_SAMPLE_RATE)).toBeLessThanOrEqual(2);
    // Decoded mulaw 0x55 has a known non-zero PCM value — the caller
    // channel must carry energy, not silence.
    const expected = mulawToPcm16(Buffer.from([0x55])).readInt16LE(0);
    expect(expected).not.toBe(0);
    expect(wav.left).toContain(expected);
    expect(uniq(wav.right)).not.toEqual([0]);
  });

  it('resamples pcm16 24k agent audio', () => {
    const dir = mkTmp();
    const rec = new LocalCallRecorder(path.join(dir, 'r.wav'));
    // 480 samples @24k == 20 ms == 320 samples @16k
    rec.addAgentAudio(pcmFrame(5, 480), 'pcm16_24k');
    rec.addCallerAudio(pcmFrame(1)); // 20 ms clock
    rec.close();

    const wav = readWav(path.join(dir, 'r.wav'));
    expect(wav.frames).toBe(FRAME_SAMPLES_20MS);
    expect(wav.right.some((v) => v !== 0)).toBe(true);
  });

  it('carries odd-length PCM chunks without corrupting samples', () => {
    const dir = mkTmp();
    const rec = new LocalCallRecorder(path.join(dir, 'r.wav'));
    const blob = pcmFrame(99, 100); // 200 bytes
    rec.addCallerAudio(blob.subarray(0, 33));
    rec.addCallerAudio(blob.subarray(33));
    rec.close();
    const wav = readWav(path.join(dir, 'r.wav'));
    expect(wav.frames).toBe(100);
    expect(uniq(wav.left)).toEqual([99]);
  });

  it('disables itself (no throw) on an unknown encoding', () => {
    const dir = mkTmp();
    const rec = new LocalCallRecorder(path.join(dir, 'r.wav'));
    rec.addCallerAudio(Buffer.from([0, 0]), 'vorbis' as never);
    // Recorder disabled itself; subsequent calls are no-ops, close() → null.
    rec.addCallerAudio(pcmFrame(1));
    expect(rec.close()).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Bounded buffers + buffered writes
// ---------------------------------------------------------------------------

describe('LocalCallRecorder — bounded buffers', () => {
  it('caps the agent backlog with a forced flush (nothing lost)', () => {
    const dir = mkTmp();
    const rec = new LocalCallRecorder(path.join(dir, 'r.wav'));
    // Shrink the cap so the test stays fast: 100 ms of backlog max.
    rec.maxBacklogBytes = (RECORDING_SAMPLE_RATE / 10) * 2;
    for (let i = 0; i < 20; i++) {
      rec.addAgentAudio(pcmFrame(3)); // 400 ms total, no caller frames
      const backlogBytes = (rec as unknown as { agentBacklogBytes: number })
        .agentBacklogBytes;
      expect(backlogBytes).toBeLessThanOrEqual(rec.maxBacklogBytes);
    }
    expect(rec.framesWritten).toBeGreaterThan(0); // overflow was committed
    rec.close();
    const wav = readWav(path.join(dir, 'r.wav'));
    expect(wav.frames).toBe(20 * FRAME_SAMPLES_20MS); // nothing lost
    expect(uniq(wav.left)).toEqual([0]);
    expect(uniq(wav.right)).toEqual([3]);
  });

  it('defaults the backlog cap to AGENT_BACKLOG_CAP_S seconds', () => {
    const dir = mkTmp();
    const rec = new LocalCallRecorder(path.join(dir, 'r.wav'));
    expect(rec.maxBacklogBytes).toBe(AGENT_BACKLOG_CAP_S * RECORDING_SAMPLE_RATE * 2);
    rec.close();
  });

  it('issues no per-frame disk I/O (64 KiB write batching)', () => {
    const dir = mkTmp();
    const p = path.join(dir, 'r.wav');
    const rec = new LocalCallRecorder(p);
    for (let i = 0; i < 10; i++) rec.addCallerAudio(pcmFrame(5)); // 12.8 KB
    expect(rec.framesWritten).toBe(10 * FRAME_SAMPLES_20MS); // logically appended
    expect(fs.statSync(p).size).toBe(44); // ... but not yet hitting disk
    rec.close();
    expect(fs.statSync(p).size).toBe(44 + 10 * FRAME_SAMPLES_20MS * 4);
  });

  it('flushes the write buffer to disk once full', () => {
    const dir = mkTmp();
    const p = path.join(dir, 'r.wav');
    const rec = new LocalCallRecorder(p);
    // 64 KiB / 1280 B per 20 ms stereo frame = 51.2 frames; 60 frames
    // guarantees at least one real disk write before close.
    for (let i = 0; i < 60; i++) rec.addCallerAudio(pcmFrame(5));
    expect(fs.statSync(p).size).toBeGreaterThan(44);
    rec.close();
    const wav = readWav(p);
    expect(wav.frames).toBe(60 * FRAME_SAMPLES_20MS);
    expect(uniq(wav.left)).toEqual([5]);
  });
});

// ---------------------------------------------------------------------------
// Finalization + teardown robustness
// ---------------------------------------------------------------------------

describe('LocalCallRecorder — finalization', () => {
  it('patches the placeholder header on close', () => {
    const dir = mkTmp();
    const p = path.join(dir, 'r.wav');
    const rec = new LocalCallRecorder(p);
    rec.addCallerAudio(pcmFrame(1));
    // Before close the placeholder header carries zero sizes.
    let header = fs.readFileSync(p);
    expect(header.readUInt32LE(40)).toBe(0);
    rec.close();
    header = fs.readFileSync(p);
    const dataSize = header.readUInt32LE(40);
    expect(dataSize).toBe(FRAME_SAMPLES_20MS * 4); // stereo 16-bit
    expect(header.readUInt32LE(4)).toBe(36 + dataSize);
  });

  it('leaves a parseable WAV on truncated teardown', () => {
    const dir = mkTmp();
    const p = path.join(dir, 'r.wav');
    const rec = new LocalCallRecorder(p);
    rec.addCallerAudio(pcmFrame(11));
    rec.addAgentAudio(pcmFrame(-11)); // still buffered — simulates mid-call
    rec.close(); // the abnormal-teardown cleanup path

    const wav = readWav(p);
    // Caller frame + close-drained agent tail are both present.
    expect(wav.frames).toBe(2 * FRAME_SAMPLES_20MS);
    expect(wav.right).toContain(-11);
  });

  it('close() is idempotent and returns the path', () => {
    const dir = mkTmp();
    const p = path.join(dir, 'r.wav');
    const rec = new LocalCallRecorder(p);
    rec.addCallerAudio(pcmFrame(1));
    const first = rec.close();
    const second = rec.close();
    expect(first).toBe(p);
    expect(second).toBe(p);
    expect(rec.closed).toBe(true);
  });

  it('ignores adds after close', () => {
    const dir = mkTmp();
    const p = path.join(dir, 'r.wav');
    const rec = new LocalCallRecorder(p);
    rec.addCallerAudio(pcmFrame(1));
    rec.close();
    rec.addCallerAudio(pcmFrame(2));
    rec.addAgentAudio(pcmFrame(2));
    expect(readWav(p).frames).toBe(FRAME_SAMPLES_20MS);
  });

  it('disables itself without raising when the descriptor dies mid-call', () => {
    const dir = mkTmp();
    const rec = new LocalCallRecorder(path.join(dir, 'r.wav'));
    fs.closeSync((rec as unknown as { fd: number }).fd); // fd dies mid-call
    // Adds buffer in userspace; the failure surfaces at the next batched
    // flush (60 frames > 64 KiB) and must disable the recorder, not throw.
    for (let i = 0; i < 60; i++) rec.addCallerAudio(pcmFrame(1));
    expect((rec as unknown as { broken: boolean }).broken).toBe(true);
    expect(rec.close()).toBeNull();
  });

  it('tracks committed duration in seconds', () => {
    const dir = mkTmp();
    const rec = new LocalCallRecorder(path.join(dir, 'r.wav'));
    for (let i = 0; i < 25; i++) rec.addCallerAudio(pcmFrame(1)); // 0.5 s
    expect(rec.durationSeconds).toBeCloseTo(0.5, 5);
    rec.close();
  });
});
