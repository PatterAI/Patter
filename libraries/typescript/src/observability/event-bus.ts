/**
 * Lightweight in-process event bus for Patter call lifecycle events.
 *
 * Mirrors the Python ``PatterEventBus`` (sdk-py/getpatter/observability/event_bus.py).
 * Consumers subscribe with ``on()`` and receive typed payloads.  ``emit()`` is
 * synchronous but handles async listeners: rejections are surfaced via the
 * Patter logger rather than being swallowed or crashing the call.
 */

import { getLogger } from '../logger';

export type PatterEventType =
  | 'turn_started'
  | 'turn_ended'
  | 'eou_metrics'
  | 'interruption'
  | 'llm_metrics'
  | 'tts_metrics'
  | 'stt_metrics'
  | 'metrics_collected'
  | 'call_ended'
  // Fine-grained pipeline events (additive — existing callbacks remain).
  | 'transcript_partial'
  | 'transcript_final'
  | 'llm_chunk'
  | 'tts_chunk'
  | 'tool_call_started';

type Listener<T = unknown> = (payload: T) => void | Promise<void>;

export class EventBus {
  private readonly listeners = new Map<PatterEventType, Set<Listener>>();

  /**
   * Subscribe to an event type.  Returns an unsubscribe function.
   */
  on<T = unknown>(event: PatterEventType, cb: Listener<T>): () => void {
    let set = this.listeners.get(event);
    if (!set) {
      set = new Set();
      this.listeners.set(event, set);
    }
    set.add(cb as Listener);
    return () => set!.delete(cb as Listener);
  }

  /**
   * Emit an event synchronously.  Async listeners are fire-and-forget with
   * rejection logging so a badly-behaved observer never stalls the call path.
   */
  emit<T = unknown>(event: PatterEventType, payload: T): void {
    const set = this.listeners.get(event);
    if (!set) return;
    for (const cb of [...set]) {
      try {
        const res = cb(payload);
        if (res && typeof (res as Promise<unknown>).catch === 'function') {
          (res as Promise<unknown>).catch((e) =>
            getLogger().error(`[EventBus] listener for "${event}" rejected:`, e),
          );
        }
      } catch (e) {
        getLogger().error(`[EventBus] listener for "${event}" threw:`, e);
      }
    }
  }
}
