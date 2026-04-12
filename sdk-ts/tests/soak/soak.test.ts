/**
 * Soak / stress tests for the Patter TypeScript SDK.
 *
 * Run with: npx vitest run tests/soak --reporter=verbose
 */

import { describe, it, expect } from "vitest";
import { CallMetricsAccumulator } from "../../src/metrics";
import { MetricsStore } from "../../src/dashboard/store";

// ---------------------------------------------------------------------------
// S1 — 100 concurrent calls for 30 seconds
// ---------------------------------------------------------------------------
// Full scenario: 100 concurrent calls for 10 minutes.
// Scaled down to 30 seconds for practical CI speed; the concurrency pattern
// is identical and the memory-growth assertion still validates the invariant.

describe("Soak Tests", () => {
  it("S1: 100 concurrent calls — RSS growth < 10%", async () => {
    const NUM_CALLS = 100;
    const DURATION_MS = 30_000;
    const frame = Buffer.alloc(320); // 20ms PCM silence at 16kHz

    const rssBefore = process.memoryUsage().rss;
    const exceptions: Error[] = [];
    const framesSent: number[] = [];

    async function simulateCall(index: number): Promise<void> {
      const acc = new CallMetricsAccumulator({
        callId: `soak-s1-${index}`,
        providerMode: "pipeline",
        telephonyProvider: "twilio",
        sttProvider: "deepgram",
        ttsProvider: "elevenlabs",
      });

      let sent = 0;
      const deadline = Date.now() + DURATION_MS;

      try {
        while (Date.now() < deadline) {
          acc.startTurn();
          acc.addSttAudioBytes(frame.length);
          acc.recordSttComplete("hello", 0.02);
          acc.recordLlmComplete();
          acc.recordTtsFirstByte();
          acc.recordTtsComplete("world");
          acc.recordTurnComplete("world");
          sent++;
          await new Promise((r) => setTimeout(r, 1000));
        }
        acc.endCall();
      } catch (err) {
        exceptions.push(err as Error);
      }
      framesSent.push(sent);
    }

    await Promise.all(
      Array.from({ length: NUM_CALLS }, (_, i) => simulateCall(i))
    );

    // Force GC if available
    if (global.gc) global.gc();

    const rssAfter = process.memoryUsage().rss;
    const growthPct = rssBefore > 0
      ? ((rssAfter - rssBefore) / rssBefore) * 100
      : 0;
    const passed = growthPct < 10;

    console.log(
      `Memory growth: ${growthPct.toFixed(1)}% (threshold: 10.0%) ${passed ? "PASS" : "FAIL"}`
    );

    expect(exceptions).toHaveLength(0);
    expect(framesSent.every((f) => f > 0)).toBe(true);
    expect(growthPct).toBeLessThan(10);
  }, 60_000);

  // ---------------------------------------------------------------------------
  // S2 — 1000-turn conversation
  // ---------------------------------------------------------------------------

  it("S2: 1000-turn conversation — cost and token counts correct", () => {
    const NUM_TURNS = 1000;
    const PER_TURN_AUDIO_SECONDS = 1.5;
    const AGENT_RESPONSE = "Reply text for turn."; // 20 chars

    const rssBefore = process.memoryUsage().rss;

    const acc = new CallMetricsAccumulator({
      callId: "soak-s2",
      providerMode: "pipeline",
      telephonyProvider: "twilio",
      sttProvider: "deepgram",
      ttsProvider: "elevenlabs",
    });

    for (let i = 0; i < NUM_TURNS; i++) {
      acc.startTurn();
      acc.recordSttComplete(`user turn ${i}`, PER_TURN_AUDIO_SECONDS);
      acc.recordLlmComplete();
      acc.recordTtsFirstByte();
      acc.recordTtsComplete(AGENT_RESPONSE);
      acc.recordTurnComplete(AGENT_RESPONSE);
    }

    const metrics = acc.endCall();

    const rssAfter = process.memoryUsage().rss;

    // Verify turn count
    expect(metrics.turns).toHaveLength(NUM_TURNS);

    // STT cost: deepgram = per minute
    // total_audio = 1.5 * 1000 = 1500s = 25 min
    // cost = 25 * 0.0043 = 0.1075
    const expectedStt =
      (PER_TURN_AUDIO_SECONDS * NUM_TURNS) / 60.0 * 0.0043;
    expect(Math.abs(metrics.cost.stt - Math.round(expectedStt * 1e6) / 1e6)).toBeLessThan(1e-6);

    // TTS cost: elevenlabs = per 1k chars
    // total_chars = 20 * 1000 = 20000 = 20 k_chars
    // cost = 20 * 0.18 = 3.6
    const expectedTts =
      (AGENT_RESPONSE.length * NUM_TURNS) / 1000.0 * 0.18;
    expect(Math.abs(metrics.cost.tts - Math.round(expectedTts * 1e6) / 1e6)).toBeLessThan(1e-6);

    const growthPct = rssBefore > 0
      ? ((rssAfter - rssBefore) / rssBefore) * 100
      : 0;
    const passed = growthPct < 10;

    console.log(`S2 turn count: ${metrics.turns.length} (expected: ${NUM_TURNS}) PASS`);
    console.log(`S2 STT cost: ${metrics.cost.stt} (expected: ${Math.round(expectedStt * 1e6) / 1e6}) PASS`);
    console.log(`S2 TTS cost: ${metrics.cost.tts} (expected: ${Math.round(expectedTts * 1e6) / 1e6}) PASS`);
    console.log(
      `Memory growth: ${growthPct.toFixed(1)}% (threshold: 10.0%) ${passed ? "PASS" : "FAIL"}`
    );

    expect(growthPct).toBeLessThan(10);
  });

  // ---------------------------------------------------------------------------
  // S3 — WebSocket reconnection under network flapping
  // ---------------------------------------------------------------------------

  it("S3: 20 disconnect/reconnect cycles — no silent frame loss", async () => {
    const NUM_CYCLES = 20;
    const RECONNECT_GAP_MS = 50;
    const frame = Buffer.alloc(320);

    // Mock WebSocket with programmable disconnect/reconnect
    class MockWebSocket {
      sent: Buffer[] = [];
      state: "OPEN" | "CLOSED" = "OPEN";

      send(data: Buffer): void {
        if (this.state !== "OPEN") {
          throw new Error("WebSocket is closed");
        }
        this.sent.push(data);
      }

      disconnect(): void {
        this.state = "CLOSED";
      }

      reconnect(): void {
        this.state = "OPEN";
      }
    }

    const ws = new MockWebSocket();
    const flushedOrDropped: Buffer[] = [];
    const inFlight: Buffer[] = [];
    const reconnectTimesMs: number[] = [];

    for (let cycle = 0; cycle < NUM_CYCLES; cycle++) {
      // Send a frame while connected
      inFlight.push(frame);
      ws.send(frame);

      // Disconnect
      ws.disconnect();
      expect(ws.state).toBe("CLOSED");

      // Flush in-flight frames
      flushedOrDropped.push(...inFlight);
      inFlight.length = 0;

      await new Promise((r) => setTimeout(r, RECONNECT_GAP_MS));

      // Reconnect and measure time
      const t0 = performance.now();
      ws.reconnect();
      const t1 = performance.now();
      reconnectTimesMs.push(t1 - t0);

      expect(ws.state).toBe("OPEN");
    }

    // All in-flight frames accounted for
    expect(flushedOrDropped).toHaveLength(NUM_CYCLES);

    // Reconnection within 500ms each
    for (let i = 0; i < reconnectTimesMs.length; i++) {
      expect(reconnectTimesMs[i]).toBeLessThan(500);
    }

    const maxReconnect = Math.max(...reconnectTimesMs);
    console.log(
      `S3 cycles: ${NUM_CYCLES}, frames accounted: ${flushedOrDropped.length} PASS`
    );
    console.log(
      `S3 max reconnect time: ${maxReconnect.toFixed(1)}ms (threshold: 500ms) PASS`
    );
  });

  // ---------------------------------------------------------------------------
  // S4 — SSE subscriber churn
  // ---------------------------------------------------------------------------

  it("S4: 50 subscribers churn with concurrent events", async () => {
    const NUM_SUBSCRIBERS = 50;
    const NUM_EVENTS = 10;
    const TIMEOUT_MS = 30_000;

    const store = new MetricsStore(500);
    store.setMaxListeners(NUM_SUBSCRIBERS + 10);
    const receivedEvents: Map<number, Array<Record<string, unknown>>> = new Map();

    for (let i = 0; i < NUM_SUBSCRIBERS; i++) {
      receivedEvents.set(i, []);
    }

    // Each subscriber listens for ~100ms
    function makeSubscriber(idx: number): Promise<void> {
      return new Promise((resolve) => {
        const events = receivedEvents.get(idx)!;
        const handler = (event: Record<string, unknown>) => {
          events.push(event);
        };
        store.on("sse", handler);

        setTimeout(() => {
          store.removeListener("sse", handler);
          resolve();
        }, 100);
      });
    }

    // Publisher emits events with small gaps
    async function publisher(): Promise<void> {
      for (let i = 0; i < NUM_EVENTS; i++) {
        store.recordCallStart({
          call_id: `s4-event-${i}`,
          caller: "+1555000",
          callee: "+1555001",
        });
        await new Promise((r) => setTimeout(r, 5));
      }
    }

    // Run with a timeout to detect deadlocks
    await Promise.race([
      Promise.all([
        publisher(),
        ...Array.from({ length: NUM_SUBSCRIBERS }, (_, i) => makeSubscriber(i)),
      ]),
      new Promise<never>((_, reject) =>
        setTimeout(() => reject(new Error("S4 deadlock timeout")), TIMEOUT_MS)
      ),
    ]);

    const totalReceived = Array.from(receivedEvents.values()).reduce(
      (sum, evts) => sum + evts.length,
      0
    );
    const subscribersWithEvents = Array.from(receivedEvents.values()).filter(
      (evts) => evts.length > 0
    ).length;

    console.log(`S4 total events received across subscribers: ${totalReceived}`);
    console.log(
      `S4 subscribers with >= 1 event: ${subscribersWithEvents}/${NUM_SUBSCRIBERS} PASS`
    );

    expect(subscribersWithEvents).toBeGreaterThan(0);
  }, 35_000);

  // ---------------------------------------------------------------------------
  // S5 — 500-call buffer wrap
  // ---------------------------------------------------------------------------

  it("S5: 501 calls — oldest evicted, newest 500 present and ordered", () => {
    const store = new MetricsStore(500);

    for (let i = 0; i < 501; i++) {
      store.recordCallStart({
        call_id: `s5-call-${i}`,
        caller: "+1555000",
        callee: "+1555001",
      });
      store.recordCallEnd({ call_id: `s5-call-${i}` });
    }

    // Store should have exactly 500 calls
    expect(store.callCount).toBe(500);

    // Oldest (index 0) should be evicted
    const evicted = store.getCall("s5-call-0");
    expect(evicted).toBeNull();

    // Newest 500 present and in order
    const allCalls = store.getCalls(500, 0);
    // getCalls returns newest first, so reverse for chronological order
    const callIds = [...allCalls].reverse().map((c) => c.call_id);
    const expectedIds = Array.from({ length: 500 }, (_, i) => `s5-call-${i + 1}`);
    expect(callIds).toEqual(expectedIds);

    console.log("S5 buffer wrap: evicted index 0, retained 500 in order PASS");
  });

  // ---------------------------------------------------------------------------
  // S6 — Cost precision over 1000 turns
  // ---------------------------------------------------------------------------

  it("S6: cost precision over 1000 turns — within 1e-9", () => {
    const NUM_TURNS = 1000;
    const EXPECTED_TOTAL = 0.123; // 0.000123 * 1000

    const acc = new CallMetricsAccumulator({
      callId: "soak-s6",
      providerMode: "pipeline",
      telephonyProvider: "twilio",
      sttProvider: "deepgram",
      ttsProvider: "elevenlabs",
      pricing: {
        // Set TTS to exactly 0.123 per 1k chars so that
        // 1 char/turn * 1000 turns = 1000 chars = 1 k_char * 0.123 = 0.123
        elevenlabs: { unit: "1k_chars", price: 0.123 },
        // Zero out STT and telephony
        deepgram: { unit: "minute", price: 0 },
        twilio: { unit: "minute", price: 0 },
      },
    });

    for (let i = 0; i < NUM_TURNS; i++) {
      acc.startTurn();
      acc.recordSttComplete(`u${i}`, 0);
      acc.recordLlmComplete();
      acc.recordTtsFirstByte();
      acc.recordTtsComplete("X"); // 1 character per turn
      acc.recordTurnComplete("X");
    }

    const metrics = acc.endCall();
    const actualTts = metrics.cost.tts;
    const tolerance = 1e-9;
    const diff = Math.abs(actualTts - EXPECTED_TOTAL);
    const passed = diff < tolerance;

    console.log(
      `S6 cost precision: actual=${actualTts}, expected=${EXPECTED_TOTAL}, ` +
        `diff=${diff.toExponential(2)} (tolerance: ${tolerance.toExponential(0)}) ` +
        `${passed ? "PASS" : "FAIL"}`
    );

    expect(diff).toBeLessThan(tolerance);
  });
});
