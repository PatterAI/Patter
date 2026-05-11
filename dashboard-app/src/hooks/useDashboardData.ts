// Live dashboard state: calls list, aggregates, and SSE stream wiring.
//
// Strategy:
//   1. On mount, fetch the initial snapshot in parallel
//      (active + recent + aggregates).
//   2. Open EventSource('/api/dashboard/events') and re-fetch the snapshot
//      whenever a relevant event arrives.
//   3. If SSE drops, reconnect with exponential backoff (1s -> 30s, 5
//      attempts) and then fall back to polling every 5 seconds.

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  fetchActiveCalls,
  fetchAggregates,
  fetchCalls,
  type Aggregates,
  type CallRecord,
} from '../lib/api';
import { toUiCall, type Call } from '../lib/mappers';

export interface DashboardData {
  readonly calls: Call[];
  readonly aggregates: Aggregates | null;
  readonly isStreaming: boolean;
  readonly error: string | null;
  readonly refresh: () => Promise<void>;
}

const RECONNECT_INITIAL_MS = 1_000;
const RECONNECT_CAP_MS = 30_000;
const RECONNECT_MAX_ATTEMPTS = 5;
const POLL_FALLBACK_MS = 5_000;

const RELEVANT_EVENTS = [
  'call_start',
  'call_initiated',
  'call_status',
  'call_end',
] as const;

function mergeCalls(active: CallRecord[], recent: CallRecord[]): Call[] {
  const seen = new Set<string>();
  const merged: Call[] = [];
  for (const record of active) {
    if (seen.has(record.call_id)) continue;
    seen.add(record.call_id);
    merged.push(toUiCall(record));
  }
  for (const record of recent) {
    if (seen.has(record.call_id)) continue;
    seen.add(record.call_id);
    merged.push(toUiCall(record));
  }
  return merged;
}

/**
 * Merge a fresh snapshot of calls with the previous state, preserving any
 * "rich" fields (transcripts, latency percentiles, cost breakdown) that the
 * fresh payload happens to omit.
 *
 * The SDK-side ``MetricsStore.updateCallStatus`` may write a synthetic
 * terminal record with ``metrics: undefined`` ahead of the canonical
 * ``recordCallEnd`` write — when a Twilio statusCallback arrives before the
 * WS ``stop`` frame. Without this merge, a live SSE refresh that pulled the
 * synthetic record would wipe transcripts + latency from the prior call in
 * the UI. ``next.field ?? prev.field`` per critical field masks the race
 * window. See ``store.ts`` TODO(0.6.2) for the root-cause fix.
 */
function mergeCallPreserving(prev: Call[], next: Call[]): Call[] {
  const prevById = new Map(prev.map((c) => [c.id, c]));
  return next.map((nc) => {
    const pc = prevById.get(nc.id);
    if (!pc) return nc;
    return {
      ...pc,
      ...nc,
      latencyP95: nc.latencyP95 ?? pc.latencyP95,
      latencyP50: nc.latencyP50 ?? pc.latencyP50,
      sttAvg: nc.sttAvg ?? pc.sttAvg,
      ttsAvg: nc.ttsAvg ?? pc.ttsAvg,
      llmAvg: nc.llmAvg ?? pc.llmAvg,
      turnCount: nc.turnCount ?? pc.turnCount,
      agentResponseP50: nc.agentResponseP50 ?? pc.agentResponseP50,
      agentResponseP95: nc.agentResponseP95 ?? pc.agentResponseP95,
      cost: { ...pc.cost, ...nc.cost },
    };
  });
}

function describeError(err: unknown): string {
  if (err instanceof Error) return err.message;
  return 'Unknown error';
}

export function useDashboardData(): DashboardData {
  const [calls, setCalls] = useState<Call[]>([]);
  const [aggregates, setAggregates] = useState<Aggregates | null>(null);
  const [isStreaming, setIsStreaming] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const mountedRef = useRef<boolean>(true);
  const eventSourceRef = useRef<EventSource | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pollTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const reconnectAttemptsRef = useRef<number>(0);

  const clearReconnectTimer = useCallback(() => {
    if (reconnectTimerRef.current !== null) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
  }, []);

  const clearPollTimer = useCallback(() => {
    if (pollTimerRef.current !== null) {
      clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  }, []);

  const closeEventSource = useCallback(() => {
    if (eventSourceRef.current !== null) {
      eventSourceRef.current.close();
      eventSourceRef.current = null;
    }
  }, []);

  const refresh = useCallback(async (): Promise<void> => {
    try {
      const [active, recent, aggs] = await Promise.all([
        fetchActiveCalls(),
        fetchCalls(50, 0),
        fetchAggregates(),
      ]);
      if (!mountedRef.current) return;
      setCalls((prev) => mergeCallPreserving(prev, mergeCalls(active, recent)));
      setAggregates(aggs);
      setError(null);
    } catch (err) {
      if (!mountedRef.current) return;
      setError(describeError(err));
    }
  }, []);

  const startPollingFallback = useCallback(() => {
    if (pollTimerRef.current !== null) return;
    pollTimerRef.current = setInterval(() => {
      void refresh();
    }, POLL_FALLBACK_MS);
  }, [refresh]);

  // Forward declaration via ref so the SSE setup callback can call itself
  // recursively for reconnects without a TDZ.
  const connectRef = useRef<() => void>(() => {});

  const scheduleReconnect = useCallback(() => {
    clearReconnectTimer();
    if (reconnectAttemptsRef.current >= RECONNECT_MAX_ATTEMPTS) {
      startPollingFallback();
      return;
    }
    const attempt = reconnectAttemptsRef.current;
    const delay = Math.min(
      RECONNECT_CAP_MS,
      RECONNECT_INITIAL_MS * Math.pow(2, attempt),
    );
    reconnectAttemptsRef.current = attempt + 1;
    reconnectTimerRef.current = setTimeout(() => {
      reconnectTimerRef.current = null;
      if (!mountedRef.current) return;
      connectRef.current();
    }, delay);
  }, [clearReconnectTimer, startPollingFallback]);

  const handleRelevantEvent = useCallback(() => {
    void refresh();
  }, [refresh]);

  const connect = useCallback(() => {
    closeEventSource();
    let source: EventSource;
    try {
      source = new EventSource('/api/dashboard/events');
    } catch (err) {
      setError(describeError(err));
      scheduleReconnect();
      return;
    }
    eventSourceRef.current = source;

    source.onopen = () => {
      if (!mountedRef.current) return;
      reconnectAttemptsRef.current = 0;
      clearPollTimer();
      setIsStreaming(true);
    };

    source.onerror = () => {
      if (!mountedRef.current) return;
      setIsStreaming(false);
      closeEventSource();
      scheduleReconnect();
    };

    for (const eventName of RELEVANT_EVENTS) {
      source.addEventListener(eventName, handleRelevantEvent);
    }
    // turn_complete updates a single call; the simplest correct behaviour is
    // to re-fetch the snapshot, same as call_status. Per-call fetching for
    // transcripts lives in useTranscript.
    source.addEventListener('turn_complete', handleRelevantEvent);
  }, [closeEventSource, clearPollTimer, handleRelevantEvent, scheduleReconnect]);

  // Keep the latest connect callback reachable from setTimeout callbacks.
  useEffect(() => {
    connectRef.current = connect;
  }, [connect]);

  useEffect(() => {
    mountedRef.current = true;
    void refresh();
    connect();
    return () => {
      mountedRef.current = false;
      clearReconnectTimer();
      clearPollTimer();
      closeEventSource();
    };
    // refresh + connect are stable (useCallback); we want this to run once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return { calls, aggregates, isStreaming, error, refresh };
}
