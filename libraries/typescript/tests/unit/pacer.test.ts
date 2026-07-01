/**
 * Unit tests for the outbound wall-clock frame pacer (mirrors Python
 * test_pacer.py): precomputed silence frames, fixed 20 ms re-framing, and a
 * drift-corrected loop that emits silence on an empty queue and bounds catch-up
 * after an oversleep. The loop uses an injected fake clock + sleep so timing is
 * deterministic.
 */

import { describe, it, expect } from 'vitest';
import {
  FRAME_MS,
  OutboundFramePacer,
  mulawSilenceFrame,
  pcm16SilenceFrame,
} from '../../src/audio/pacer';

describe('[unit] pacer silence frames', () => {
  it('mu-law silence is 160 bytes of 0xFF', () => {
    const f = mulawSilenceFrame();
    expect(f.equals(Buffer.alloc(160, 0xff))).toBe(true);
    expect(f.length).toBe(160);
  });

  it('PCM16 silence is zeroed and rate-sized', () => {
    expect(pcm16SilenceFrame(16000).equals(Buffer.alloc(640, 0))).toBe(true);
    expect(pcm16SilenceFrame(8000).equals(Buffer.alloc(320, 0))).toBe(true);
  });
});

describe('[unit] pacer re-framing', () => {
  it('returns silence when empty', () => {
    const silence = mulawSilenceFrame();
    const pacer = new OutboundFramePacer({ frameBytes: 160, silenceFrame: silence });
    expect(pacer.nextFrame().equals(silence)).toBe(true);
  });

  it('slices exact frame bytes and pads the sub-frame remainder', () => {
    const silence = mulawSilenceFrame();
    const pacer = new OutboundFramePacer({ frameBytes: 160, silenceFrame: silence });
    pacer.enqueue(Buffer.alloc(400, 0x01)); // 2.5 frames
    expect(pacer.nextFrame().equals(Buffer.alloc(160, 0x01))).toBe(true);
    expect(pacer.nextFrame().equals(Buffer.alloc(160, 0x01))).toBe(true);
    // 80 bytes remain -> padded with the silence tail, then drained.
    const third = pacer.nextFrame();
    expect(third.equals(Buffer.concat([Buffer.alloc(80, 0x01), silence.subarray(80)]))).toBe(true);
    expect(pacer.nextFrame().equals(silence)).toBe(true);
  });

  it('accumulates enqueues across calls', () => {
    const silence = pcm16SilenceFrame(16000);
    const pacer = new OutboundFramePacer({ frameBytes: 640, silenceFrame: silence });
    pacer.enqueue(Buffer.alloc(300, 0x02));
    pacer.enqueue(Buffer.alloc(340, 0x02)); // 640 total -> one exact frame
    expect(pacer.nextFrame().equals(Buffer.alloc(640, 0x02))).toBe(true);
    expect(pacer.nextFrame().equals(silence)).toBe(true);
  });

  it('reports pending bytes', () => {
    const pacer = new OutboundFramePacer({ frameBytes: 160, silenceFrame: mulawSilenceFrame() });
    expect(pacer.pendingBytes).toBe(0);
    pacer.enqueue(Buffer.alloc(250, 0x01));
    expect(pacer.pendingBytes).toBe(250);
  });
});

class FakeClock {
  t = 1000.0;
  now = (): number => this.t;
  sleep = async (delay: number): Promise<void> => {
    this.t += Math.max(0, delay);
  };
}

describe('[unit] pacer loop', () => {
  it('emits one frame per frame period and keeps the clock alive with silence', async () => {
    const clock = new FakeClock();
    const sent: Buffer[] = [];
    const pacer = new OutboundFramePacer({
      frameBytes: 160,
      silenceFrame: mulawSilenceFrame(),
      sendFrame: async (f) => {
        sent.push(f);
      },
      clock: clock.now,
      sleep: clock.sleep,
    });
    pacer.enqueue(Buffer.alloc(160 * 3, 0x01)); // exactly 3 real frames

    await pacer.run({ maxFrames: 5 });

    expect(sent[0].equals(Buffer.alloc(160, 0x01))).toBe(true);
    expect(sent[2].equals(Buffer.alloc(160, 0x01))).toBe(true);
    expect(sent[3].equals(mulawSilenceFrame())).toBe(true);
    expect(sent[4].equals(mulawSilenceFrame())).toBe(true);
    expect(clock.t).toBeCloseTo(1000.0 + 5 * (FRAME_MS / 1000), 6);
  });

  it('bounds catch-up after an oversleep', async () => {
    const clock = new FakeClock();
    const sent: Buffer[] = [];
    let jumped = false;
    const jumpySleep = async (delay: number): Promise<void> => {
      if (!jumped) {
        jumped = true;
        clock.t += 0.1; // 5 frame periods of oversleep
      } else {
        clock.t += Math.max(0, delay);
      }
    };
    const pacer = new OutboundFramePacer({
      frameBytes: 160,
      silenceFrame: mulawSilenceFrame(),
      sendFrame: async (f) => {
        sent.push(f);
      },
      clock: clock.now,
      sleep: jumpySleep,
      maxCatchupFrames: 3,
    });
    await pacer.run({ maxFrames: 10 });
    expect(sent.length).toBe(10); // terminated cleanly, no runaway burst
  });

  it('stop halts the loop at exactly the stopping tick', async () => {
    const clock = new FakeClock();
    const sent: Buffer[] = [];
    let pacer: OutboundFramePacer;
    const send = async (f: Buffer): Promise<void> => {
      sent.push(f);
      if (sent.length === 2) pacer.stop();
    };
    pacer = new OutboundFramePacer({
      frameBytes: 160,
      silenceFrame: mulawSilenceFrame(),
      sendFrame: send,
      clock: clock.now,
      sleep: clock.sleep,
    });
    await pacer.run({ maxFrames: 100 });
    expect(sent.length).toBe(2);
  });
});
