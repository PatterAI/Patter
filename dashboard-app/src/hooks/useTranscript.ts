// Hook returning the mapped transcript for a single call.
//
// Behaviour:
//   - When `callId` is null, returns [] and does nothing.
//   - When `callId` changes, fetches the call once and maps the transcript.
//   - When `isLive` is true, polls the call detail endpoint every 2 seconds
//     so newly streamed turns become visible without an SSE subscription
//     here (the parent useDashboardData owns the SSE connection).

import { useEffect, useRef, useState } from 'react';
import { fetchCall } from '../lib/api';
import { toUiTranscript, type TranscriptTurn } from '../lib/mappers';

const LIVE_POLL_MS = 2_000;

export function useTranscript(
  callId: string | null,
  isLive: boolean,
): TranscriptTurn[] {
  const [turns, setTurns] = useState<TranscriptTurn[]>([]);
  const mountedRef = useRef<boolean>(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    if (!callId) {
      setTurns([]);
      return;
    }

    let cancelled = false;
    let pollTimer: ReturnType<typeof setInterval> | null = null;

    const load = async (): Promise<void> => {
      try {
        const record = await fetchCall(callId);
        if (cancelled || !mountedRef.current) return;
        if (record === null) {
          setTurns([]);
          return;
        }
        setTurns(toUiTranscript(record));
      } catch {
        // Swallow transient errors; the next poll tick will retry. The
        // dashboard-level error surface lives in useDashboardData.
      }
    };

    void load();

    if (isLive) {
      pollTimer = setInterval(() => {
        void load();
      }, LIVE_POLL_MS);
    }

    return () => {
      cancelled = true;
      if (pollTimer !== null) {
        clearInterval(pollTimer);
      }
    };
  }, [callId, isLive]);

  return turns;
}
