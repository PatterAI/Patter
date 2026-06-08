import { describe, it, expect, vi } from 'vitest';
import { MetricsStore } from '../src/dashboard/store';

describe('MetricsStore', () => {
  it('records call start and tracks active calls', () => {
    const store = new MetricsStore();
    store.recordCallStart({ call_id: 'c1', caller: '+1111', callee: '+2222', direction: 'inbound' });

    const active = store.getActiveCalls();
    expect(active).toHaveLength(1);
    expect(active[0].call_id).toBe('c1');
    expect(active[0].caller).toBe('+1111');
  });

  it('records call end and moves to completed', () => {
    const store = new MetricsStore();
    store.recordCallStart({ call_id: 'c1', caller: '+1111', callee: '+2222' });
    store.recordCallEnd({ call_id: 'c1', transcript: [] }, { cost: { total: 0.05 } });

    expect(store.getActiveCalls()).toHaveLength(0);
    const calls = store.getCalls();
    expect(calls).toHaveLength(1);
    expect(calls[0].call_id).toBe('c1');
    expect(calls[0].metrics).toEqual({ cost: { total: 0.05 } });
  });

  it('records turns on active calls', () => {
    const store = new MetricsStore();
    store.recordCallStart({ call_id: 'c1', caller: '+1111', callee: '+2222' });
    store.recordTurn({ call_id: 'c1', turn: { turn_index: 0, user_text: 'Hello' } });

    const active = store.getActiveCalls();
    expect(active[0].turns).toHaveLength(1);
  });

  it('enforces max calls limit', () => {
    const store = new MetricsStore(3);
    for (let i = 0; i < 5; i++) {
      store.recordCallStart({ call_id: `c${i}` });
      store.recordCallEnd({ call_id: `c${i}` });
    }
    expect(store.callCount).toBe(3);
  });

  it('getCalls returns newest first', () => {
    const store = new MetricsStore();
    store.recordCallStart({ call_id: 'c1' });
    store.recordCallEnd({ call_id: 'c1' });
    store.recordCallStart({ call_id: 'c2' });
    store.recordCallEnd({ call_id: 'c2' });

    const calls = store.getCalls();
    expect(calls[0].call_id).toBe('c2');
    expect(calls[1].call_id).toBe('c1');
  });

  it('getCall returns specific call', () => {
    const store = new MetricsStore();
    store.recordCallStart({ call_id: 'c1' });
    store.recordCallEnd({ call_id: 'c1' });

    expect(store.getCall('c1')).not.toBeNull();
    expect(store.getCall('nonexistent')).toBeNull();
  });

  it('computes aggregates', () => {
    const store = new MetricsStore();
    store.recordCallStart({ call_id: 'c1' });
    store.recordCallEnd({ call_id: 'c1' }, {
      cost: { total: 0.10, stt: 0.01, tts: 0.05, llm: 0.02, telephony: 0.02 },
      duration_seconds: 60,
      latency_avg: { total_ms: 500 },
    } as Record<string, unknown>);

    const agg = store.getAggregates();
    expect(agg.total_calls).toBe(1);
    expect(agg.total_cost).toBe(0.1);
    expect(agg.avg_duration).toBe(60);
    expect(agg.avg_latency_ms).toBe(500);
  });

  it('returns empty aggregates for no calls', () => {
    const store = new MetricsStore();
    const agg = store.getAggregates();
    expect(agg.total_calls).toBe(0);
    expect(agg.total_cost).toBe(0);
  });

  it('emits SSE events', () => {
    const store = new MetricsStore();
    const events: unknown[] = [];
    store.on('sse', (event) => events.push(event));

    store.recordCallStart({ call_id: 'c1', caller: '+1' });
    store.recordTurn({ call_id: 'c1', turn: { text: 'hello' } });
    store.recordCallEnd({ call_id: 'c1' });

    expect(events).toHaveLength(3);
    expect((events[0] as { type: string }).type).toBe('call_start');
    expect((events[1] as { type: string }).type).toBe('turn_complete');
    expect((events[2] as { type: string }).type).toBe('call_end');
  });

  it('filters calls by time range', () => {
    const store = new MetricsStore();
    store.recordCallStart({ call_id: 'c1' });
    store.recordCallEnd({ call_id: 'c1' });

    const now = Date.now() / 1000;
    const result = store.getCallsInRange(now - 10, now + 10);
    expect(result).toHaveLength(1);

    const noResult = store.getCallsInRange(now + 100, now + 200);
    expect(noResult).toHaveLength(0);
  });

  it('getCallsInRange returns all calls when no bounds given', () => {
    const store = new MetricsStore();
    store.recordCallStart({ call_id: 'c1' });
    store.recordCallEnd({ call_id: 'c1' });
    store.recordCallStart({ call_id: 'c2' });
    store.recordCallEnd({ call_id: 'c2' });

    const result = store.getCallsInRange();
    expect(result).toHaveLength(2);
  });

  it('SSE subscriber receives correct event data', () => {
    const store = new MetricsStore();
    const events: Array<{ type: string; data: Record<string, unknown> }> = [];
    store.on('sse', (event) => events.push(event));

    store.recordCallStart({ call_id: 'x1', caller: '+100', callee: '+200', direction: 'outbound' });

    expect(events).toHaveLength(1);
    expect(events[0].type).toBe('call_start');
    expect(events[0].data.caller).toBe('+100');
    expect(events[0].data.direction).toBe('outbound');
  });

  it('max calls cap keeps only the newest entries', () => {
    const store = new MetricsStore(2);
    for (let i = 0; i < 4; i++) {
      store.recordCallStart({ call_id: `c${i}` });
      store.recordCallEnd({ call_id: `c${i}` });
    }

    expect(store.callCount).toBe(2);
    const calls = store.getCalls();
    expect(calls[0].call_id).toBe('c3');
    expect(calls[1].call_id).toBe('c2');
  });

  it('ignores recordCallStart with empty call_id', () => {
    const store = new MetricsStore();
    store.recordCallStart({ call_id: '' });
    expect(store.getActiveCalls()).toHaveLength(0);
  });

  it('ignores recordTurn with missing turn data', () => {
    const store = new MetricsStore();
    store.recordCallStart({ call_id: 'c1' });
    store.recordTurn({ call_id: 'c1' }); // no turn field
    const active = store.getActiveCalls();
    expect(active[0].turns).toHaveLength(0);
  });

  // BUG 1 — live-transcript accumulates user/assistant lines across multiple
  // round-trips. Without this behaviour the SPA's mapper had to derive a
  // running transcript from ``turns[]`` and the primary mapper path
  // (``record.transcript.length > 0``) was empty for live calls — producing
  // intermittent renderings where one turn replaced the previous one.
  it('recordTurn appends both user and assistant lines to active.transcript across turns', () => {
    const store = new MetricsStore();
    store.recordCallStart({ call_id: 'c1', caller: '+1', callee: '+2' });

    // Turn 0: agent's first message (no user_text yet).
    store.recordTurn({
      call_id: 'c1',
      turn: { turn_index: 0, user_text: '', agent_text: 'Hello!', timestamp: 1 },
    });
    // Turn 1: user → agent round-trip.
    store.recordTurn({
      call_id: 'c1',
      turn: { turn_index: 1, user_text: 'Hi there', agent_text: 'How can I help?', timestamp: 2 },
    });
    // Turn 2: another user → agent round-trip.
    store.recordTurn({
      call_id: 'c1',
      turn: { turn_index: 2, user_text: 'Tell me a joke', agent_text: 'Why did the chicken…', timestamp: 3 },
    });

    const active = store.getActive('c1');
    expect(active).toBeDefined();
    expect(active!.turns).toHaveLength(3);
    // Five entries: bot/Hello + user/Hi+bot/Howcan + user/joke+bot/Why
    expect(active!.transcript).toHaveLength(5);
    expect(active!.transcript![0]).toEqual({ role: 'assistant', text: 'Hello!', timestamp: 1, turnIndex: 0 });
    expect(active!.transcript![1]).toEqual({ role: 'user', text: 'Hi there', timestamp: 2, turnIndex: 1 });
    expect(active!.transcript![2]).toEqual({ role: 'assistant', text: 'How can I help?', timestamp: 2, turnIndex: 1 });
    expect(active!.transcript![3]).toEqual({ role: 'user', text: 'Tell me a joke', timestamp: 3, turnIndex: 2 });
    expect(active!.transcript![4]).toEqual({ role: 'assistant', text: 'Why did the chicken…', timestamp: 3, turnIndex: 2 });
  });

  it("recordTurn skips '[interrupted]' agent_text and empty user_text from active.transcript", () => {
    const store = new MetricsStore();
    store.recordCallStart({ call_id: 'c1' });

    // Empty user_text + non-empty agent_text → only assistant line is pushed.
    store.recordTurn({
      call_id: 'c1',
      turn: { turn_index: 0, user_text: '', agent_text: 'Greeting', timestamp: 1 },
    });
    // Interrupted turn — agent_text === '[interrupted]' is filtered out.
    store.recordTurn({
      call_id: 'c1',
      turn: { turn_index: 1, user_text: 'wait', agent_text: '[interrupted]', timestamp: 2 },
    });

    const active = store.getActive('c1');
    expect(active!.transcript).toHaveLength(2);
    expect(active!.transcript![0]).toEqual({ role: 'assistant', text: 'Greeting', timestamp: 1, turnIndex: 0 });
    expect(active!.transcript![1]).toEqual({ role: 'user', text: 'wait', timestamp: 2, turnIndex: 1 });
  });

  // BUG 2 — completed entries preserve transcript and turns from the active
  // record so the live-pane race window between updateCallStatus
  // ('completed') and recordCallEnd never yields a blank record.
  it("updateCallStatus('completed') copies turns and transcript from active record", () => {
    const store = new MetricsStore();
    store.recordCallStart({ call_id: 'c1', caller: '+1', callee: '+2' });
    store.recordTurn({
      call_id: 'c1',
      turn: { turn_index: 0, user_text: 'Hi', agent_text: 'Hello', timestamp: 5 },
    });
    store.updateCallStatus('c1', 'completed', { duration_seconds: 12 });

    const completed = store.getCall('c1');
    expect(completed).not.toBeNull();
    expect(completed!.status).toBe('completed');
    // turns + running transcript carried over so the dashboard's live
    // pane has data to render in the gap between this event and the
    // subsequent recordCallEnd.
    expect(completed!.turns).toHaveLength(1);
    expect(completed!.transcript).toHaveLength(2);
    expect(completed!.transcript![0]).toEqual({ role: 'user', text: 'Hi', timestamp: 5, turnIndex: 0 });
    expect(completed!.transcript![1]).toEqual({ role: 'assistant', text: 'Hello', timestamp: 5, turnIndex: 0 });
  });

  it("recordCallEnd preserves active turns and falls back to running transcript when data.transcript is empty", () => {
    const store = new MetricsStore();
    store.recordCallStart({ call_id: 'c1', caller: '+1', callee: '+2' });
    store.recordTurn({
      call_id: 'c1',
      turn: { turn_index: 0, user_text: 'A', agent_text: 'B', timestamp: 1 },
    });
    // Carrier statusCallback fires first, moves to completed without
    // populating the transcript field.
    store.updateCallStatus('c1', 'completed', {});
    // Then the WS-driven recordCallEnd runs WITHOUT a transcript payload
    // (e.g. an external controller calling end_call early). The fallback
    // should pull the running transcript / turns from the prior entry.
    store.recordCallEnd({ call_id: 'c1' });

    const completed = store.getCall('c1');
    expect(completed!.transcript).toHaveLength(2);
    expect(completed!.turns).toHaveLength(1);
  });

  it('recordCallEnd prefers explicit data.transcript over the running fallback', () => {
    const store = new MetricsStore();
    store.recordCallStart({ call_id: 'c1' });
    store.recordTurn({
      call_id: 'c1',
      turn: { turn_index: 0, user_text: 'live-A', agent_text: 'live-B', timestamp: 1 },
    });
    const authoritative = [
      { role: 'user', text: 'final-A', timestamp: 10 },
      { role: 'assistant', text: 'final-B', timestamp: 11 },
    ];
    store.recordCallEnd({ call_id: 'c1', transcript: authoritative });

    const completed = store.getCall('c1');
    expect(completed!.transcript).toEqual(authoritative);
  });
});

describe('MetricsStore.hydrate', () => {
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const fs = require('node:fs') as typeof import('node:fs');
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const os = require('node:os') as typeof import('node:os');

  function buildFixture(
    root: string,
    calls: Array<{ id: string; iso: string; meta?: Record<string, unknown> }>,
  ): void {
    for (const c of calls) {
      const date = new Date(c.iso);
      const yyyy = String(date.getUTCFullYear()).padStart(4, '0');
      const mm = String(date.getUTCMonth() + 1).padStart(2, '0');
      const dd = String(date.getUTCDate()).padStart(2, '0');
      const dir = `${root}/calls/${yyyy}/${mm}/${dd}/${c.id}`;
      fs.mkdirSync(dir, { recursive: true });
      const metadata = {
        call_id: c.id,
        caller: '+15550001111',
        callee: '+15550002222',
        direction: 'outbound',
        started_at: date.toISOString(),
        ended_at: new Date(date.getTime() + 30_000).toISOString(),
        status: 'completed',
        metrics: { p95_latency_ms: 1500 },
        ...(c.meta ?? {}),
      };
      fs.writeFileSync(`${dir}/metadata.json`, JSON.stringify(metadata));
    }
  }

  it('returns 0 when logRoot is null/undefined/missing', () => {
    const store = new MetricsStore();
    expect(store.hydrate(null)).toBe(0);
    expect(store.hydrate(undefined)).toBe(0);
    expect(store.hydrate(`/tmp/nonexistent-${Math.random()}`)).toBe(0);
    expect(store.callCount).toBe(0);
  });

  it('rebuilds the call list from on-disk metadata files', () => {
    const root = fs.mkdtempSync(`${os.tmpdir()}/patter-store-test-`);
    try {
      buildFixture(root, [
        { id: 'CA-old', iso: '2026-04-25T10:00:00.000Z' },
        { id: 'CA-new', iso: '2026-04-26T15:30:00.000Z' },
      ]);
      const store = new MetricsStore();
      expect(store.hydrate(root)).toBe(2);
      const list = store.getCalls();
      expect(list[0].call_id).toBe('CA-new'); // newest first
      expect(list[1].call_id).toBe('CA-old');
      expect(list[0].metrics).toEqual({ p95_latency_ms: 1500 });
      expect(list[0].direction).toBe('outbound');
      expect(list[0].status).toBe('completed');
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it('skips already-known call_ids (idempotent on re-hydrate)', () => {
    const root = fs.mkdtempSync(`${os.tmpdir()}/patter-store-test-`);
    try {
      buildFixture(root, [{ id: 'CA-1', iso: '2026-04-26T15:00:00.000Z' }]);
      const store = new MetricsStore();
      expect(store.hydrate(root)).toBe(1);
      expect(store.hydrate(root)).toBe(0);
      expect(store.callCount).toBe(1);
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it('tolerates corrupt metadata.json without aborting other entries', () => {
    const root = fs.mkdtempSync(`${os.tmpdir()}/patter-store-test-`);
    try {
      buildFixture(root, [{ id: 'CA-good', iso: '2026-04-26T15:00:00.000Z' }]);
      const badDir = `${root}/calls/2026/04/26/CA-bad`;
      fs.mkdirSync(badDir, { recursive: true });
      fs.writeFileSync(`${badDir}/metadata.json`, '{ not valid json');
      const store = new MetricsStore();
      expect(store.hydrate(root)).toBe(1);
      expect(store.getCalls()[0].call_id).toBe('CA-good');
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it('respects maxCalls when hydrating large histories', () => {
    const root = fs.mkdtempSync(`${os.tmpdir()}/patter-store-test-`);
    try {
      const calls = Array.from({ length: 7 }, (_, i) => ({
        id: `CA-${i}`,
        iso: `2026-04-26T15:0${i}:00.000Z`,
      }));
      buildFixture(root, calls);
      const store = new MetricsStore({ maxCalls: 3 });
      expect(store.hydrate(root)).toBe(7);
      const list = store.getCalls();
      expect(list).toHaveLength(3);
      expect(list[0].call_id).toBe('CA-6');
      expect(list[2].call_id).toBe('CA-4');
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it('skips records with unparseable started_at (no silent epoch-0 insert)', () => {
    const root = fs.mkdtempSync(`${os.tmpdir()}/patter-store-test-`);
    try {
      buildFixture(root, [{ id: 'CA-good', iso: '2026-04-26T15:00:00.000Z' }]);
      const badDir = `${root}/calls/2026/04/26/CA-bad`;
      fs.mkdirSync(badDir, { recursive: true });
      fs.writeFileSync(
        `${badDir}/metadata.json`,
        JSON.stringify({
          call_id: 'CA-bad',
          caller: '+1',
          callee: '+2',
          started_at: 'not-a-date',
        }),
      );

      const store = new MetricsStore();
      expect(store.hydrate(root)).toBe(1);
      const list = store.getCalls();
      expect(list).toHaveLength(1);
      expect(list[0].call_id).toBe('CA-good');
      expect(list.find((c) => c.call_id === 'CA-bad')).toBeUndefined();
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it('accepts numeric (Unix-seconds) timestamps in metadata', () => {
    const root = fs.mkdtempSync(`${os.tmpdir()}/patter-store-test-`);
    try {
      const callDir = `${root}/calls/2026/04/26/CA-numeric`;
      fs.mkdirSync(callDir, { recursive: true });
      fs.writeFileSync(
        `${callDir}/metadata.json`,
        JSON.stringify({
          call_id: 'CA-numeric',
          caller: '+1',
          callee: '+2',
          started_at: 1745683200,
          ended_at: 1745683230,
          status: 'completed',
        }),
      );

      const store = new MetricsStore();
      expect(store.hydrate(root)).toBe(1);
      const list = store.getCalls();
      expect(list[0].started_at).toBe(1745683200);
      expect(list[0].ended_at).toBe(1745683230);
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it('lifts top-level cost/latency/duration into metrics (CallLogger schema)', () => {
    // CallLogger.logCallEnd writes cost/latency/duration_ms/telephony_provider
    // at the top of metadata.json — without this fallback hydrated calls show
    // $0.00 / "—" in the dashboard because the UI reads from metrics.cost etc.
    const root = fs.mkdtempSync(`${os.tmpdir()}/patter-store-test-`);
    try {
      const callDir = `${root}/calls/2026/05/08/CA-real-shape`;
      fs.mkdirSync(callDir, { recursive: true });
      fs.writeFileSync(
        `${callDir}/metadata.json`,
        JSON.stringify({
          schema_version: '1.0',
          call_id: 'CA-real-shape',
          started_at: '2026-05-08T23:33:00.000Z',
          ended_at: '2026-05-08T23:33:57.000Z',
          duration_ms: 57400,
          status: 'completed',
          telephony_provider: 'twilio',
          provider_mode: 'pipeline',
          turns: 9,
          cost: {
            stt: 0.001526,
            tts: 0.02988,
            llm: 0.000406,
            telephony: 0.0085,
            total: 0.040312,
          },
          latency: { p50_ms: 2127.7, p95_ms: 3461.7, p99_ms: 3640.1 },
        }),
      );
      const store = new MetricsStore();
      expect(store.hydrate(root)).toBe(1);
      const rec = store.getCalls()[0];
      expect(rec.metrics).not.toBeNull();
      const m = rec.metrics as Record<string, unknown>;
      expect((m.cost as Record<string, number>).total).toBeCloseTo(0.040312, 6);
      expect((m.latency as Record<string, number>).p95_ms).toBeCloseTo(3461.7);
      expect((m.latency_avg as Record<string, number>).total_ms).toBeCloseTo(3461.7);
      expect(m.duration_seconds).toBeCloseTo(57.4);
      expect(m.telephony_provider).toBe('twilio');
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it('preserves explicit metrics when present (does not overwrite with top-level)', () => {
    const root = fs.mkdtempSync(`${os.tmpdir()}/patter-store-test-`);
    try {
      const callDir = `${root}/calls/2026/05/08/CA-explicit`;
      fs.mkdirSync(callDir, { recursive: true });
      fs.writeFileSync(
        `${callDir}/metadata.json`,
        JSON.stringify({
          call_id: 'CA-explicit',
          started_at: '2026-05-08T10:00:00Z',
          metrics: { cost: { total: 0.999 }, marker: 'kept' },
          cost: { total: 0.001 },
          latency: { p95_ms: 9999 },
        }),
      );
      const store = new MetricsStore();
      expect(store.hydrate(root)).toBe(1);
      const m = store.getCalls()[0].metrics as Record<string, unknown>;
      expect(m.marker).toBe('kept');
      expect((m.cost as Record<string, number>).total).toBeCloseTo(0.999);
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });
});

describe('MetricsStore — recordCallEnd does not duplicate after updateCallStatus', () => {
  // Regression for dashboard BUG C: the Twilio statusCallback for
  // ``CallStatus=completed`` invokes ``updateCallStatus`` (which moves the
  // row from active to completed), and then the WS ``stop`` frame invokes
  // ``recordCallEnd`` for the same call_id. Before the fix the second
  // call appended a duplicate row with ``started_at=0`` and empty
  // caller/callee, which then masked the original entry in ``getCalls``
  // (newest-first ordering, mergeCalls de-dup keeps the first match).
  it('updates the existing entry instead of appending a duplicate', () => {
    const store = new MetricsStore();
    store.recordCallInitiated({
      call_id: 'CA-dup',
      caller: '+15551112222',
      callee: '+15553334444',
      direction: 'outbound',
    });
    store.recordCallStart({ call_id: 'CA-dup' });
    // Twilio statusCallback path moves the call to completed first.
    store.updateCallStatus('CA-dup', 'completed', { duration_seconds: 42 });
    expect(store.getActiveCalls()).toHaveLength(0);
    expect(store.callCount).toBe(1);
    const intermediate = store.getCalls()[0];
    expect(intermediate.caller).toBe('+15551112222');
    expect(intermediate.callee).toBe('+15553334444');
    const startedAtBefore = intermediate.started_at;
    expect(startedAtBefore).toBeGreaterThan(0);

    // Then the WS stop handler fires recordCallEnd. ``data.caller`` is
    // empty here because outbound TwiML carries no Stream parameters.
    store.recordCallEnd(
      { call_id: 'CA-dup', caller: '', callee: '', transcript: [] },
      { cost: { total: 0.07 }, duration_seconds: 42 } as Record<string, unknown>,
    );

    expect(store.callCount).toBe(1); // no duplicate row
    const finalEntry = store.getCalls()[0];
    expect(finalEntry.call_id).toBe('CA-dup');
    expect(finalEntry.caller).toBe('+15551112222'); // preserved
    expect(finalEntry.callee).toBe('+15553334444');
    expect(finalEntry.started_at).toBe(startedAtBefore); // preserved (not 0)
    expect(finalEntry.metrics).toEqual({
      cost: { total: 0.07 },
      duration_seconds: 42,
    });
    expect(finalEntry.status).toBe('completed');
  });

  it('keeps a call inside the 24h time-range window after end', () => {
    // End-to-end check that mirrors the real bug: dashboard-app filters
    // calls by [now - 24h, now] using ``startedAtMs``. With the duplicate
    // bug the started_at was 0 → call dropped off the 24h slice.
    const store = new MetricsStore();
    store.recordCallInitiated({
      call_id: 'CA-window',
      caller: '+15551112222',
      callee: '+15553334444',
      direction: 'outbound',
    });
    store.recordCallStart({ call_id: 'CA-window' });
    store.updateCallStatus('CA-window', 'completed', { duration_seconds: 5 });
    store.recordCallEnd(
      { call_id: 'CA-window', transcript: [] },
      { duration_seconds: 5 } as Record<string, unknown>,
    );
    const now = Date.now() / 1000;
    const inWindow = store.getCallsInRange(now - 86_400, now + 60);
    expect(inWindow.map((c) => c.call_id)).toContain('CA-window');
  });

  // --- FIX-5 (issue #154): live per-line transcript + (turnIndex, role) dedup ---

  it('recordTranscriptLine appends a live line to the active transcript and publishes SSE', () => {
    const store = new MetricsStore();
    const events: Array<{ type: string; data: Record<string, unknown> }> = [];
    store.on('sse', (e) => events.push(e));
    store.recordCallStart({ call_id: 'c1', caller: '+1111', callee: '+2222' });

    store.recordTranscriptLine({ call_id: 'c1', turnIndex: 0, role: 'user', text: 'What time is it?' });

    const active = store.getActive('c1');
    expect(active?.transcript).toHaveLength(1);
    expect(active?.transcript?.[0]).toMatchObject({ role: 'user', text: 'What time is it?', turnIndex: 0 });

    const sse = events.find((e) => e.type === 'transcript_line');
    expect(sse).toBeDefined();
    expect(sse?.data).toMatchObject({ call_id: 'c1', turnIndex: 0, role: 'user', text: 'What time is it?' });
  });

  it('recordTranscriptLine ignores tool roles, empty text, and unknown calls', () => {
    const store = new MetricsStore();
    store.recordCallStart({ call_id: 'c1' });
    store.recordTranscriptLine({ call_id: 'c1', turnIndex: 0, role: 'tool' as 'user', text: 'x' });
    store.recordTranscriptLine({ call_id: 'c1', turnIndex: 0, role: 'user', text: '' });
    store.recordTranscriptLine({ call_id: 'nope', turnIndex: 0, role: 'user', text: 'hi' });
    expect(store.getActive('c1')?.transcript ?? []).toHaveLength(0);
  });

  it('recordTurn does NOT duplicate a line already emitted live by (turnIndex, role)', () => {
    const store = new MetricsStore();
    store.recordCallStart({ call_id: 'c1' });
    // Live lines first (the forward path), both on turn 0.
    store.recordTranscriptLine({ call_id: 'c1', turnIndex: 0, role: 'user', text: 'Hello' });
    store.recordTranscriptLine({ call_id: 'c1', turnIndex: 0, role: 'assistant', text: 'Hi there' });
    // Metrics turn for the same index arrives later — must not re-push.
    store.recordTurn({
      call_id: 'c1',
      turn: { turn_index: 0, user_text: 'Hello', agent_text: 'Hi there' },
    });
    const transcript = store.getActive('c1')?.transcript ?? [];
    expect(transcript).toHaveLength(2);
    expect(transcript.map((e) => `${e.role}:${e.text}`)).toEqual([
      'user:Hello',
      'assistant:Hi there',
    ]);
  });

  it('recordTurn still mirrors text when no live line was emitted for that turn', () => {
    const store = new MetricsStore();
    store.recordCallStart({ call_id: 'c1' });
    store.recordTurn({
      call_id: 'c1',
      turn: { turn_index: 0, user_text: 'Hello', agent_text: 'Hi there' },
    });
    const transcript = store.getActive('c1')?.transcript ?? [];
    expect(transcript).toHaveLength(2);
    expect(transcript[0]).toMatchObject({ role: 'user', text: 'Hello', turnIndex: 0 });
    expect(transcript[1]).toMatchObject({ role: 'assistant', text: 'Hi there', turnIndex: 0 });
  });
});
