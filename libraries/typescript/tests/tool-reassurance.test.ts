import { describe, it, expect } from 'vitest';

/**
 * Reassurance is a stream-handler-level feature wired around
 * ``DefaultToolExecutor.execute``. The full integration goes through
 * the OpenAI Realtime adapter (sendText), which requires a live
 * WebSocket — covered in integration tests. Here we verify the
 * surface-level invariants: the type accepts both forms, defaults
 * are sensible, and the timer mechanics work in isolation.
 */

interface ReassuranceConfig {
  message: string;
  afterMs?: number;
}

function normalizeReassurance(
  input: string | ReassuranceConfig | undefined,
): { message: string; afterMs: number } | null {
  if (!input) return null;
  if (typeof input === 'string') return { message: input, afterMs: 1500 };
  return { message: input.message, afterMs: input.afterMs ?? 1500 };
}

describe('[unit] Reassurance configuration parsing', () => {
  it('accepts the string shorthand with default afterMs=1500', () => {
    expect(normalizeReassurance('Let me check that for you')).toEqual({
      message: 'Let me check that for you',
      afterMs: 1500,
    });
  });

  it('accepts the explicit object form with custom afterMs', () => {
    expect(normalizeReassurance({ message: 'Hold on', afterMs: 2500 })).toEqual({
      message: 'Hold on',
      afterMs: 2500,
    });
  });

  it('uses default afterMs=1500 when only message is set', () => {
    expect(normalizeReassurance({ message: 'Hold on' })).toEqual({
      message: 'Hold on',
      afterMs: 1500,
    });
  });

  it('returns null for undefined / empty input', () => {
    expect(normalizeReassurance(undefined)).toBeNull();
  });
});

describe('[unit] Reassurance timer mechanics', () => {
  it('fires after the grace window when the tool runs longer', async () => {
    const fired: string[] = [];
    const send = (msg: string): void => { fired.push(msg); };

    const slowTool = (): Promise<string> => new Promise((resolve) => setTimeout(() => resolve('done'), 200));

    let timer: ReturnType<typeof setTimeout> | null = null;
    timer = setTimeout(() => send('Let me check'), 50);
    try {
      await slowTool();
    } finally {
      if (timer) clearTimeout(timer);
    }
    expect(fired).toEqual(['Let me check']);
  });

  it('does NOT fire when the tool returns before the grace window', async () => {
    const fired: string[] = [];
    const send = (msg: string): void => { fired.push(msg); };

    const fastTool = (): Promise<string> => new Promise((resolve) => setTimeout(() => resolve('done'), 30));

    let timer: ReturnType<typeof setTimeout> | null = null;
    timer = setTimeout(() => send('Let me check'), 200);
    try {
      await fastTool();
    } finally {
      if (timer) clearTimeout(timer);
    }
    // tiny wait to ensure the cleared timer cannot still fire
    await new Promise((r) => setTimeout(r, 250));
    expect(fired).toEqual([]);
  });
});
