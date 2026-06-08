// Hook returning the mapped transcript for a single call.
//
// Behaviour:
//   - When `callId` is null, returns [] and does nothing.
//   - When `callId` changes, fetches the call once and maps the transcript.
//   - Subscribes to SSE for the currently-selected call regardless of
//     ``isLive`` so the transition from in-progress → ended is observed
//     directly:
//       * ``turn_complete`` (live calls) — refetch on each completed
//         round-trip, primary signal during the call.
//       * ``call_end`` — refetch once when the call hangs up so the pane
//         picks up the SDK-authoritative ``history.entries`` transcript
//         the moment ``recordCallEnd`` lands. Without this the pane could
//         go blank in the race window between the carrier statusCallback
//         (``completed``) and the WS-driven ``recordCallEnd``. See
//         dashboard BUG 2.
//   - When ``isLive`` is true, also polls the call detail endpoint every
//     2 s as a backstop in case SSE drops on a flaky network.
//   - All SSE handlers refetch the full call rather than appending the
//     event payload directly: the call detail endpoint is the single
//     source of truth (it merges the active record's ``turns`` /
//     ``transcript`` and any persisted ``transcript``), so we never have
//     to reason about ordering or de-duplication on the client.

import { useEffect, useRef, useState } from 'react';
import { fetchCall, withToken } from '../lib/api';
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
    let source: EventSource | null = null;

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
        // Swallow transient errors; the next poll tick (or the next SSE
        // event) will retry. Dashboard-level error surface lives in
        // useDashboardData.
      }
    };

    void load();

    // Filter SSE payloads to the currently-selected call. The SSE stream is
    // a shared bus across every dashboard tab and every active call, so
    // without this filter we'd refetch on every event of every unrelated
    // call. ``MessageEvent.data`` is JSON: parse and compare ``call_id``
    // before triggering a reload.
    const isForThisCall = (ev: Event): boolean => {
      const messageEvent = ev as MessageEvent<string>;
      try {
        const payload = JSON.parse(messageEvent.data) as { call_id?: unknown };
        return payload?.call_id === callId;
      } catch {
        return false;
      }
    };

    try {
      source = new EventSource(withToken('/api/dashboard/events'));
      // ``turn_complete`` is the per-round-trip metrics signal; refetching
      // keeps the pane in sync as turns accumulate.
      source.addEventListener('turn_complete', (ev) => {
        if (!isForThisCall(ev)) return;
        void load();
      });
      // ``transcript_line`` (FIX-5, issue #154) is the live per-line signal —
      // the Realtime handler emits one the moment each user/assistant line is
      // known, BEFORE the turn completes. Refetching pulls the line (the call
      // detail endpoint already merges it into the active record's
      // ``transcript``); ``toUiTranscript`` then re-sorts by
      // (turnIndex, user<assistant) so a late user line lands above its agent
      // line. This makes the user utterance appear immediately rather than
      // only on turn completion.
      source.addEventListener('transcript_line', (ev) => {
        if (!isForThisCall(ev)) return;
        void load();
      });
      // ``call_end`` fires the moment ``recordCallEnd`` lands the
      // SDK-authoritative ``history.entries`` transcript onto the call
      // record. Refetching here closes the race window where the pane
      // would otherwise display the pre-end snapshot (potentially empty
      // when the carrier statusCallback ran first). Subscribing
      // unconditionally — even when ``isLive`` is false — covers the case
      // where the call ended a moment after the user selected the row but
      // before the dashboard list refreshed ``isLive`` to false.
      source.addEventListener('call_end', (ev) => {
        if (!isForThisCall(ev)) return;
        void load();
      });
    } catch {
      // EventSource not available (SSR / older browsers): the 2 s polling
      // fallback below keeps the pane updated for live calls.
      source = null;
    }

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
      if (source !== null) {
        source.close();
      }
    };
  }, [callId, isLive]);

  return turns;
}
