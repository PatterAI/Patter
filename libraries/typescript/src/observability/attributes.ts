/**
 * ``patter.*`` span attribute helpers — TypeScript mirror of
 * ``libraries/python/getpatter/observability/attributes.py``.
 *
 * No-op when OpenTelemetry isn't wired up. The helpers exist primarily so
 * provider adapters in both SDKs can share the same call sites without
 * each one re-implementing the "is OTel available?" gating. When tracing
 * is disabled (``PATTER_OTEL_ENABLED`` unset, ``@opentelemetry/api`` not
 * installed, or no active call scope), every helper is a fast no-op.
 *
 * Parity contract with the Python helpers:
 *   - ``record_patter_attrs``     ↔ ``recordPatterAttrs``
 *   - ``patter_call_scope``       ↔ ``patterCallScope``
 *   - ``attach_span_exporter``    ↔ ``attachSpanExporter``
 *
 * The semantics differ in one structural way: Python uses ``ContextVar``
 * to propagate the active call ID through asyncio task trees. JS has no
 * equivalent built-in, so this module uses a module-level "current call"
 * cell and a stack to support nested ``patterCallScope`` invocations on
 * the same loop. That's fine because the TS SDK has at most one active
 * call scope per Node process / per request handler in practice.
 */
import { getLogger } from '../logger';
import { ENV_FLAG, isTracingEnabled } from './tracing';

/**
 * "uut" = unit-under-test, the default value stamped on
 * ``patter.side`` when no driver/UUT split is configured.
 */
export const DEFAULT_SIDE = 'uut';

interface CallScopeFrame {
  readonly callId: string;
  readonly side: string;
}

const _scopeStack: CallScopeFrame[] = [];

function _currentScope(): CallScopeFrame | null {
  return _scopeStack.length > 0 ? _scopeStack[_scopeStack.length - 1] : null;
}

interface OtelApiShape {
  trace: {
    getTracer(name: string): {
      startSpan(
        name: string,
        options?: { attributes?: Record<string, unknown> },
      ): {
        setAttribute(key: string, value: unknown): unknown;
        end(): unknown;
        isRecording?(): boolean;
      };
    };
    getActiveSpan?(): {
      setAttribute(key: string, value: unknown): unknown;
      isRecording?(): boolean;
    } | null;
  };
}

function _tryLoadOtelApi(): OtelApiShape | null {
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    return require('@opentelemetry/api') as OtelApiShape;
  } catch {
    return null;
  }
}

/**
 * Stamp ``patter.*`` attributes on the current span, augmenting them with
 * the ambient ``patter.call_id`` / ``patter.side`` from the active
 * ``patterCallScope``. No-op when tracing is disabled or no scope is
 * active.
 *
 * Behaviour mirrors the Python helper:
 *   - If an active recording span exists, attributes are stamped on it.
 *   - Otherwise a transient zero-duration ``patter.billable`` span is
 *     opened to carry the attributes. Some collectors filter
 *     zero-duration spans; callers that need guaranteed attribution
 *     should wrap their billable work in their own span.
 *
 * Caller-provided ``patter.call_id`` / ``patter.side`` keys win over the
 * scope's values.
 */
export function recordPatterAttrs(attrs: Readonly<Record<string, unknown>>): void {
  if (!isTracingEnabled()) return;
  const scope = _currentScope();
  if (scope === null) return;

  const api = _tryLoadOtelApi();
  if (!api) return;

  const full: Record<string, unknown> = { ...attrs };
  if (full['patter.call_id'] === undefined) full['patter.call_id'] = scope.callId;
  if (full['patter.side'] === undefined) full['patter.side'] = scope.side;

  try {
    const active = api.trace.getActiveSpan?.() ?? null;
    if (active && (active.isRecording === undefined || active.isRecording())) {
      for (const [k, v] of Object.entries(full)) {
        try {
          active.setAttribute(k, v);
        } catch {
          // Swallow — OTel must never crash the call path.
        }
      }
      return;
    }
  } catch {
    // fall through to billable-span fallback
  }

  try {
    const tracer = api.trace.getTracer('getpatter.observability');
    const span = tracer.startSpan('patter.billable', { attributes: full });
    try {
      span.end();
    } catch {
      // Swallow.
    }
  } catch {
    // Swallow.
  }
}

/**
 * Bind ``callId`` and ``side`` to the active span scope for the duration
 * of ``fn``. Mirrors the Python ``patter_call_scope`` context manager:
 * any ``recordPatterAttrs`` call made inside ``fn`` (or anything ``fn``
 * awaits) sees the bound values.
 *
 * Note: JavaScript has no ContextVar equivalent, so this uses a
 * module-level stack. Concurrent overlapping scopes on the same event
 * loop will see the innermost scope's values — fine for the SDK's
 * one-call-per-handler model. If callers need true async-context
 * isolation, install ``AsyncLocalStorage``-backed propagation via the
 * OTel SDK's context manager.
 */
export async function patterCallScope<T>(
  options: { readonly callId: string; readonly side?: string },
  fn: () => Promise<T>,
): Promise<T> {
  if (!options.callId) {
    throw new Error('patterCallScope requires non-empty callId');
  }
  const frame: CallScopeFrame = {
    callId: options.callId,
    side: options.side ?? DEFAULT_SIDE,
  };
  _scopeStack.push(frame);
  try {
    return await fn();
  } finally {
    // Defensive pop: locate this exact frame in case nested scopes
    // raised and left the stack in an unexpected state.
    const idx = _scopeStack.lastIndexOf(frame);
    if (idx >= 0) _scopeStack.splice(idx, 1);
  }
}

/**
 * Wire an OTel ``SpanExporter`` into the SDK's tracer provider and
 * remember the configured ``side`` on the Patter instance so the
 * per-call handler reads it when entering ``patterCallScope``.
 *
 * Mirrors the Python ``attach_span_exporter`` contract:
 *   - Stores ``side`` on ``patterInstance._patterSide`` unconditionally
 *     (works even when ``@opentelemetry/*`` peer deps are missing).
 *   - Idempotent on the *same exporter object reference*. Two distinct
 *     exporter instances pointing at the same backend will both be
 *     attached and spans will be exported twice — hold a single
 *     exporter object across calls to avoid duplicates.
 *
 * When tracing isn't enabled (env flag off / SDK peer deps absent), the
 * call is a no-op aside from storing ``_patterSide``.
 */
export function attachSpanExporter(
  patterInstance: { _patterSide?: string } & Record<string, unknown>,
  exporter: unknown,
  options: { readonly side?: string } = {},
): void {
  const side = options.side ?? DEFAULT_SIDE;
  patterInstance._patterSide = side;

  if (!isTracingEnabled()) {
    getLogger().debug(
      `attachSpanExporter: ${ENV_FLAG} not enabled or tracer unavailable; only side= stored`,
    );
    return;
  }

  // SDK wire-up packages are optional. When absent, fall through silently
  // — Python does the same.
  let sdkTraceBase:
    | {
        BasicTracerProvider: new (opts?: unknown) => {
          addSpanProcessor?(p: unknown): void;
          _patterAttachedExporters?: Set<unknown>;
        };
        SimpleSpanProcessor: new (exporter: unknown) => unknown;
      }
    | null = null;
  let sdkTraceNode:
    | {
        NodeTracerProvider: new (opts?: unknown) => {
          addSpanProcessor?(p: unknown): void;
          _patterAttachedExporters?: Set<unknown>;
        };
      }
    | null = null;
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    sdkTraceBase = require('@opentelemetry/sdk-trace-base');
  } catch {
    sdkTraceBase = null;
  }
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    sdkTraceNode = require('@opentelemetry/sdk-trace-node');
  } catch {
    sdkTraceNode = null;
  }
  if (!sdkTraceBase) {
    getLogger().warn(
      'attachSpanExporter: @opentelemetry/sdk-trace-base is not installed; ' +
        'spans will not be exported. Install ' +
        '@opentelemetry/sdk-trace-base + @opentelemetry/sdk-trace-node.',
    );
    return;
  }

  const api = _tryLoadOtelApi();
  if (!api) return;

  let provider:
    | ({
        addSpanProcessor?(p: unknown): void;
        _patterAttachedExporters?: Set<unknown>;
      } & Record<string, unknown>)
    | null = null;

  try {
    // Prefer the existing global provider — never replace a host-app
    // TracerProvider silently (parity with Python's behaviour).
    const tracerApi = api.trace as unknown as {
      getTracerProvider?(): unknown;
    };
    const existing = tracerApi.getTracerProvider?.() ?? null;
    if (
      existing &&
      typeof (existing as { addSpanProcessor?: unknown }).addSpanProcessor === 'function'
    ) {
      provider = existing as unknown as typeof provider;
    }
  } catch {
    provider = null;
  }

  if (!provider) {
    if (!sdkTraceNode) {
      getLogger().warn(
        'attachSpanExporter: no SDK TracerProvider registered and ' +
          '@opentelemetry/sdk-trace-node is not installed; cannot wire exporter.',
      );
      return;
    }
    try {
      provider = new sdkTraceNode.NodeTracerProvider();
      const trace = api.trace as unknown as {
        setGlobalTracerProvider?(p: unknown): void;
      };
      trace.setGlobalTracerProvider?.(provider);
    } catch (e) {
      getLogger().debug(
        `attachSpanExporter: failed to construct NodeTracerProvider: ${String(
          (e as Error)?.message ?? e,
        )}`,
      );
      return;
    }
  }

  // Idempotency: track attached exporters by reference on the provider.
  let seen = provider._patterAttachedExporters;
  if (!seen) {
    seen = new Set<unknown>();
    provider._patterAttachedExporters = seen;
  }
  if (seen.has(exporter)) return;

  try {
    const processor = new sdkTraceBase.SimpleSpanProcessor(exporter);
    provider.addSpanProcessor?.(processor);
    seen.add(exporter);
  } catch (e) {
    getLogger().debug(
      `attachSpanExporter: failed to register exporter: ${String(
        (e as Error)?.message ?? e,
      )}`,
    );
  }
}

/** Internal: reset module state (primarily for tests; not part of the public API). */
export function _resetPatterAttrsForTesting(): void {
  _scopeStack.length = 0;
}
