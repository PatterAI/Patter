/**
 * OpenTelemetry tracing helpers for Patter — TypeScript mirror of
 * ``sdk-py/getpatter/observability/tracing.py``.
 *
 * Design goals:
 *   - Zero cost when disabled. ``startSpan`` falls back to a cheap no-op span
 *     unless the opt-in env var ``PATTER_OTEL_ENABLED=1`` is set *and* the
 *     optional ``@opentelemetry/api`` package is installed.
 *   - Opt-in only. No telemetry is emitted by default. We never export PII
 *     (user utterances, tool payloads) as span attributes — only sizes and
 *     provider identifiers.
 *   - Single source of truth for span names — downstream services should
 *     attribute spans using the ``SPAN_*`` constants at the bottom.
 */
import { getLogger } from '../logger';

/** Environment variable that gates all OpenTelemetry wire-up. */
export const ENV_FLAG = 'PATTER_OTEL_ENABLED';
/** Default `service.name` reported on exported spans. */
export const SERVICE_NAME = 'patter';

// --- Span names -------------------------------------------------------------
// Normalized to ``getpatter.*`` for parity with the Python SDK and to avoid
// the historical ``patter.*`` / ``getpatter.*`` mix that fragmented dashboards.
export const SPAN_CALL = 'getpatter.call';
export const SPAN_STT = 'getpatter.stt';
export const SPAN_LLM = 'getpatter.llm';
export const SPAN_TTS = 'getpatter.tts';
export const SPAN_TOOL = 'getpatter.tool';
export const SPAN_ENDPOINT = 'getpatter.endpoint';
export const SPAN_BARGEIN = 'getpatter.bargein';

/**
 * Minimal span surface area — subset of the OTel ``Span`` API the Patter SDK
 * relies on. We keep this narrow so the no-op fallback stays trivial.
 */
export interface Span {
  setAttribute(key: string, value: unknown): void;
  recordException(exception: unknown): void;
  end(): void;
}

/** Options for `initTracing()`. */
export interface InitTracingOptions {
  serviceName?: string;
  otlpEndpoint?: string;
  resourceAttributes?: Record<string, string>;
}

// --- OTel handle (lazily resolved) -----------------------------------------
interface OtelApiShape {
  trace: {
    getTracer(name: string): {
      startSpan(name: string, options?: { attributes?: Record<string, unknown> }): unknown;
    };
    setGlobalTracerProvider?(provider: unknown): void;
  };
}

/** Minimal surface of ``NodeTracerProvider`` / ``BasicTracerProvider``. */
interface TracerProviderShape {
  addSpanProcessor?(processor: unknown): void;
  register?(): void;
  shutdown?(): Promise<void>;
  forceFlush?(): Promise<void>;
}

let otel: OtelApiShape | null = null;
let initialized = false;
let tracerAvailable = false;
let provider: TracerProviderShape | null = null;

function tryLoadOtel(): OtelApiShape | null {
  if (otel !== null) return otel;
  try {
    // ``require`` keeps this an optional peer dep — bundlers that don't tree
    // shake this will still be fine because the module is only touched when
    // ``PATTER_OTEL_ENABLED`` is set.
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const mod = require('@opentelemetry/api') as OtelApiShape;
    otel = mod;
    return mod;
  } catch {
    return null;
  }
}

/**
 * Attempt to wire a ``NodeTracerProvider`` + OTLP HTTP exporter when the
 * optional SDK packages are installed. Returns ``null`` when any piece is
 * missing — callers fall back to the no-op tracer (current behaviour).
 */
function trySetupSdk(
  options: InitTracingOptions,
  api: OtelApiShape,
): TracerProviderShape | null {
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const sdkTraceNode = require('@opentelemetry/sdk-trace-node') as {
      NodeTracerProvider: new (opts?: Record<string, unknown>) => TracerProviderShape;
    };
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const sdkTraceBase = require('@opentelemetry/sdk-trace-base') as {
      BatchSpanProcessor: new (exporter: unknown) => unknown;
    };
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const otlpHttp = require('@opentelemetry/exporter-trace-otlp-http') as {
      OTLPTraceExporter: new (opts?: { url?: string }) => unknown;
    };

    const serviceName = options.serviceName ?? SERVICE_NAME;
    // Many SDK versions accept a ``resource`` object, but building one pulls
    // in yet another package. Pass service name via options when the provider
    // supports it — otherwise rely on OTEL_SERVICE_NAME env.
    const providerInstance = new sdkTraceNode.NodeTracerProvider({
      resource: {
        attributes: {
          'service.name': serviceName,
          ...(options.resourceAttributes ?? {}),
        },
      },
    });

    const endpoint =
      options.otlpEndpoint ?? process.env.OTEL_EXPORTER_OTLP_ENDPOINT ?? undefined;
    const exporter = new otlpHttp.OTLPTraceExporter(
      endpoint ? { url: `${endpoint.replace(/\/$/, '')}/v1/traces` } : undefined,
    );
    const processor = new sdkTraceBase.BatchSpanProcessor(exporter);
    providerInstance.addSpanProcessor?.(processor);
    providerInstance.register?.();

    // Best-effort: expose globally so ``api.trace.getTracer`` returns this
    // provider's tracer.
    try {
      api.trace.setGlobalTracerProvider?.(providerInstance);
    } catch {
      // Swallow — ``register()`` already sets the global in most SDKs.
    }
    return providerInstance;
  } catch (e) {
    getLogger().debug(
      `[observability] OTel SDK wire-up skipped: ${String((e as Error)?.message ?? e)}`,
    );
    return null;
  }
}

function envFlagEnabled(): boolean {
  const raw = (process.env[ENV_FLAG] ?? '').toLowerCase();
  return raw === '1' || raw === 'true' || raw === 'yes';
}

/**
 * Initialize tracing. Returns ``true`` when OTel is wired, ``false`` otherwise
 * (which covers both "env flag off" and "peer dep missing").
 *
 * If the optional SDK packages (``@opentelemetry/sdk-trace-node``,
 * ``@opentelemetry/sdk-trace-base``, ``@opentelemetry/exporter-trace-otlp-http``)
 * are installed, a ``NodeTracerProvider`` with OTLP/HTTP exporter is wired up
 * automatically. Otherwise, spans produced via ``startSpan`` are still created
 * against whatever global provider ``@opentelemetry/api`` resolves to (which
 * may be a no-op if the host hasn't registered one).
 */
export function initTracing(options: InitTracingOptions = {}): boolean {
  if (initialized) return tracerAvailable;
  initialized = true;

  if (!envFlagEnabled()) {
    tracerAvailable = false;
    return false;
  }

  const api = tryLoadOtel();
  if (!api) {
    getLogger().warn(
      `${ENV_FLAG}=1 but @opentelemetry/api is not installed. ` +
        'Install with: npm install @opentelemetry/api ' +
        '@opentelemetry/sdk-trace-node @opentelemetry/sdk-trace-base ' +
        '@opentelemetry/exporter-trace-otlp-http',
    );
    tracerAvailable = false;
    return false;
  }

  // Optional SDK wire-up — fail silently to a no-op tracer when not installed.
  provider = trySetupSdk(options, api);

  tracerAvailable = true;
  const serviceName = options.serviceName ?? SERVICE_NAME;
  getLogger().info(
    `[observability] Patter OTel tracing enabled (service=${serviceName}${
      provider ? ', exporter=otlp-http' : ', exporter=noop'
    })`,
  );
  return true;
}

/**
 * Flush any pending spans and tear down the tracer provider. Safe to call
 * unconditionally — returns immediately when tracing was never wired up.
 */
export async function shutdownTracing(): Promise<void> {
  if (provider) {
    try {
      await provider.forceFlush?.();
    } catch {
      // Swallow.
    }
    try {
      await provider.shutdown?.();
    } catch {
      // Swallow.
    }
  }
  provider = null;
  otel = null;
  initialized = false;
  tracerAvailable = false;
}

/**
 * Return the configured tracer (or a cheap no-op when tracing is disabled).
 * The returned object exposes only ``startSpan`` — callers that need richer
 * OTel APIs should import ``@opentelemetry/api`` directly.
 */
export function getTracer(name: string = SERVICE_NAME): {
  startSpan(n: string, attrs?: Record<string, unknown>): Span;
} {
  if (!isTracingEnabled() || !otel) {
    return {
      startSpan: () => NOOP_SPAN,
    };
  }
  try {
    const tracer = otel.trace.getTracer(name);
    return {
      startSpan: (spanName: string, attrs?: Record<string, unknown>) => {
        try {
          const raw = tracer.startSpan(
            spanName,
            attrs ? { attributes: attrs } : undefined,
          );
          return new RealSpan(raw as OtelSpan);
        } catch {
          return NOOP_SPAN;
        }
      },
    };
  } catch {
    return { startSpan: () => NOOP_SPAN };
  }
}

/** True only if the env flag is set AND the tracer initialized cleanly. */
export function isTracingEnabled(): boolean {
  return tracerAvailable && envFlagEnabled();
}

// --- Noop fallback ----------------------------------------------------------
class NoopSpan implements Span {
  setAttribute(_key: string, _value: unknown): void {
    // no-op
  }
  recordException(_exception: unknown): void {
    // no-op
  }
  end(): void {
    // no-op
  }
}

const NOOP_SPAN = new NoopSpan();

// --- Real span wrapper ------------------------------------------------------
interface OtelSpan {
  setAttribute(key: string, value: unknown): unknown;
  recordException(exception: unknown): unknown;
  end(): unknown;
}

class RealSpan implements Span {
  private readonly span: OtelSpan;

  constructor(span: OtelSpan) {
    this.span = span;
  }

  setAttribute(key: string, value: unknown): void {
    try {
      this.span.setAttribute(key, value);
    } catch {
      // Swallow — OTel should never crash the call path.
    }
  }

  recordException(exception: unknown): void {
    try {
      this.span.recordException(exception);
    } catch {
      // Swallow.
    }
  }

  end(): void {
    try {
      this.span.end();
    } catch {
      // Swallow.
    }
  }
}

/**
 * Start a span. Callers must ``end()`` the returned span — use try/finally:
 *
 * ```ts
 * const span = startSpan(SPAN_LLM, { 'llm.model': 'gpt-4o' });
 * try { ... } finally { span.end(); }
 * ```
 *
 * Returns a no-op span when tracing is disabled or unavailable.
 */
export function startSpan(
  name: string,
  attrs?: Record<string, unknown>,
): Span {
  if (!isTracingEnabled() || !otel) return NOOP_SPAN;
  try {
    const tracer = otel.trace.getTracer(SERVICE_NAME);
    const rawSpan = tracer.startSpan(name, attrs ? { attributes: attrs } : undefined);
    return new RealSpan(rawSpan as OtelSpan);
  } catch {
    return NOOP_SPAN;
  }
}

/**
 * Convenience wrapper — starts a span, runs ``fn``, records exceptions on
 * throw, and always ends the span (try/finally). Mirrors Python's
 * ``with start_span(...):`` context-manager ergonomics.
 *
 * ```ts
 * await withSpan(SPAN_LLM, { 'llm.model': 'gpt-4o' }, async (span) => {
 *   span.setAttribute('llm.tokens', 123);
 *   return await callLLM();
 * });
 * ```
 */
export async function withSpan<T>(
  name: string,
  attrs: Record<string, unknown> | undefined,
  fn: (span: Span) => Promise<T>,
): Promise<T> {
  const span = startSpan(name, attrs);
  try {
    return await fn(span);
  } catch (exc) {
    span.recordException(exc);
    throw exc;
  } finally {
    span.end();
  }
}

/** Internal: reset module state (primarily for tests; not part of the public API). */
export function _resetTracingForTesting(): void {
  otel = null;
  initialized = false;
  tracerAvailable = false;
  provider = null;
}
