/**
 * Smoke tests for ``patter.*`` attribute helpers.
 *
 * The real OTel wire-up lives in optional peer deps (``@opentelemetry/*``).
 * These tests confirm the public surface is callable without crashing when
 * those deps are absent — matching the no-op-by-default contract shared
 * with the Python helpers.
 */
import { describe, it, expect } from 'vitest';

import {
  recordPatterAttrs,
  patterCallScope,
  attachSpanExporter,
  DEFAULT_SIDE,
} from '../../src/observability/attributes';

describe('observability/attributes (no-op surface)', () => {
  it('exports the expected helpers with the correct shapes', () => {
    expect(typeof recordPatterAttrs).toBe('function');
    expect(typeof patterCallScope).toBe('function');
    expect(typeof attachSpanExporter).toBe('function');
    expect(DEFAULT_SIDE).toBe('uut');
  });

  it('recordPatterAttrs is a no-op when tracing is disabled', () => {
    // Tracing is disabled by default (PATTER_OTEL_ENABLED unset).
    expect(() => {
      recordPatterAttrs({ 'patter.cost.llm_usd': 0.001 });
    }).not.toThrow();
  });

  it('patterCallScope rejects empty callId', async () => {
    await expect(
      patterCallScope({ callId: '' }, async () => 0),
    ).rejects.toThrow(/callId/);
  });

  it('patterCallScope binds the scope around fn (default side)', async () => {
    let observedDuringFn = false;
    const value = await patterCallScope({ callId: 'c-1' }, async () => {
      // The helper has no public reader, but ``recordPatterAttrs`` must
      // remain a no-op inside the scope when tracing is disabled.
      recordPatterAttrs({ 'patter.cost.llm_usd': 0 });
      observedDuringFn = true;
      return 42;
    });
    expect(value).toBe(42);
    expect(observedDuringFn).toBe(true);
  });

  it('patterCallScope unwinds the stack on throw', async () => {
    await expect(
      patterCallScope({ callId: 'c-throw' }, async () => {
        throw new Error('boom');
      }),
    ).rejects.toThrow(/boom/);
    // A second scope should still work — proves the stack was unwound.
    const v = await patterCallScope({ callId: 'c-after' }, async () => 7);
    expect(v).toBe(7);
  });

  it('attachSpanExporter stores _patterSide even when OTel is disabled', () => {
    const fakePatter: { _patterSide?: string } = {};
    const fakeExporter = {};
    expect(() =>
      attachSpanExporter(fakePatter, fakeExporter, { side: 'driver' }),
    ).not.toThrow();
    expect(fakePatter._patterSide).toBe('driver');
  });

  it('attachSpanExporter defaults side to "uut"', () => {
    const fakePatter: { _patterSide?: string } = {};
    attachSpanExporter(fakePatter, {});
    expect(fakePatter._patterSide).toBe(DEFAULT_SIDE);
  });
});
