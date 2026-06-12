#!/usr/bin/env node

/**
 * Patter CLI — standalone dashboard and utilities.
 *
 * Usage:
 *   npx getpatter dashboard [--port 8000]
 *   npx getpatter eval run <suite> [--agent module:export] [--output report.json]
 *   npx getpatter telemetry [status|disable|enable]
 */

import { createServer } from 'node:http';
import express from 'express';
import { MetricsStore } from './dashboard/store';
import { mountDashboard, mountApi } from './dashboard/routes';
import { getLogger } from './logger';
import { showBanner } from './banner';
import { VERSION } from './version';
import { TelemetryClient, DEFAULT_ENDPOINT } from './telemetry/client';
import { isEnabled } from './telemetry/consent';
import { isOptedOut, setOptOut } from './telemetry/install-id';

/**
 * Record which CLI command was invoked (the name only — never args/flags), then
 * flush. `process.exit()` skips the client's `beforeExit` flush hook, so we flush
 * explicitly here, bounded so the CLI stays snappy even when the collector is
 * unreachable. Best-effort and fail-safe — never blocks or breaks the CLI.
 */
async function emitCliCommand(command: string): Promise<void> {
  try {
    const client = new TelemetryClient({ sdkVersion: VERSION });
    client.record('cli_command', { cli_command: command });
    const timeout = new Promise<void>((resolve) => {
      const t = setTimeout(resolve, 600);
      (t as { unref?: () => void }).unref?.();
    });
    await Promise.race([client.close(), timeout]);
  } catch {
    /* best-effort — never break the CLI */
  }
}

/**
 * Implement `getpatter telemetry status|disable|enable` (parity with
 * `next telemetry`). Persists a machine-level opt-out marker read by consent.
 */
function runTelemetryCommand(action: string | undefined): number {
  const act = action ?? 'status';
  if (act === 'disable') {
    try {
      setOptOut(true);
    } catch (err) {
      console.log(`Could not write the opt-out marker: ${String(err)}`);
      return 1;
    }
    console.log('Anonymous telemetry disabled. No usage data will be sent.');
    return 0;
  }
  if (act === 'enable') {
    try {
      setOptOut(false);
    } catch (err) {
      console.log(`Could not remove the opt-out marker: ${String(err)}`);
      return 1;
    }
    console.log('Anonymous telemetry re-enabled (opt-out model, on by default).');
    return 0;
  }
  if (act !== 'status') {
    console.log('Usage: getpatter telemetry [status|disable|enable]');
    return 1;
  }
  const endpoint = process.env.PATTER_TELEMETRY_ENDPOINT || DEFAULT_ENDPOINT;
  console.log(`Anonymous usage telemetry: ${isEnabled() ? 'ENABLED' : 'DISABLED'}`);
  if (isOptedOut()) {
    console.log('  Opted out via: getpatter telemetry disable (persisted marker)');
  }
  console.log(`  Endpoint: ${endpoint}`);
  console.log('  Inspect what would be sent (prints, sends nothing): PATTER_TELEMETRY_DEBUG=1');
  console.log(
    '  Disable: getpatter telemetry disable  |  DO_NOT_TRACK=1  |  PATTER_TELEMETRY_DISABLED=1',
  );
  console.log('  Details: https://docs.getpatter.com/telemetry');
  return 0;
}

function parseArgs(argv: string[]): { port: number } {
  const args = argv.slice(2);
  let port = 8000;

  for (let i = 0; i < args.length; i++) {
    if (args[i] === 'dashboard') continue;
    if (args[i] === '--port' && args[i + 1]) {
      port = parseInt(args[i + 1], 10);
      i++;
    } else if (args[i] === '--help' || args[i] === '-h') {
      console.log('Usage: getpatter dashboard [--port 8000]');
      process.exit(0);
    }
  }

  return { port };
}

/**
 * `getpatter eval run <suite>` — run an evaluation suite. Mirrors the
 * Python `patter eval run` CLI (same flags, same report shape, same exit
 * codes: 0 all passed, 1 failures, 2 usage).
 */
async function runEvalCommand(args: string[]): Promise<number> {
  if (args[0] !== 'run' || !args[1]) {
    console.log('Usage: getpatter eval run <suite> [--agent module:export]');
    console.log('       [--judge-model gpt-4o-mini] [--pass-threshold 0.7] [--output report.json]');
    return 2;
  }
  const suitePath = args[1];
  let agentSpec = '';
  let judgeModel = 'gpt-4o-mini';
  let passThreshold = 0.7;
  let output = '';
  for (let i = 2; i < args.length; i++) {
    const flag = args[i];
    const value = args[i + 1];
    if (flag === '--agent' && value !== undefined) {
      agentSpec = value;
      i++;
    } else if (flag === '--judge-model' && value !== undefined) {
      judgeModel = value;
      i++;
    } else if (flag === '--pass-threshold' && value !== undefined) {
      passThreshold = Number.parseFloat(value);
      i++;
    } else if (flag === '--output' && value !== undefined) {
      output = value;
      i++;
    } else {
      console.log(`Unknown eval option: ${flag}`);
      return 2;
    }
  }

  const { EvalRunner, LLMJudge, loadSuite } = await import('./evals');
  const suite = await loadSuite(suitePath);
  const judge = new LLMJudge({ model: judgeModel, passThreshold });
  const runner = new EvalRunner({ judge });

  const factory = await loadAgentFactory(agentSpec);
  const results = await runner.run(suite, factory);
  const report = runner.report(suite, results);

  if (output) {
    const { writeFile } = await import('node:fs/promises');
    await writeFile(output, report, 'utf-8');
    console.log(`Wrote report to ${output}`);
  } else {
    console.log(report);
  }

  const total = results.length;
  const passed = results.filter((r) => r.judge.passed).length;
  console.error(`\n${passed}/${total} cases passed`);
  return passed === total ? 0 : 1;
}

/**
 * Resolve a ``module:export`` string to an agent factory.
 *
 * Falls back to an echo-style mock agent when no factory is provided — lets
 * developers sanity-check their suite shape before wiring the real agent.
 */
async function loadAgentFactory(
  spec: string,
): Promise<() => (text: string) => Promise<string>> {
  if (!spec) {
    const echo = async (text: string): Promise<string> => `echo: ${text}`;
    return () => echo;
  }
  const sep = spec.lastIndexOf(':');
  if (sep <= 0) {
    throw new Error("--agent must be of the form 'module-or-path:exportName'");
  }
  const modulePath = spec.slice(0, sep);
  const exportName = spec.slice(sep + 1);
  const { pathToFileURL } = await import('node:url');
  const { resolve } = await import('node:path');
  const { existsSync } = await import('node:fs');
  // File paths (./agent.mjs, ../x.js, absolute) are resolved against cwd;
  // anything else is treated as a bare module specifier.
  const looksLikePath =
    modulePath.startsWith('.') || modulePath.startsWith('/') || existsSync(modulePath);
  const specifier = looksLikePath
    ? pathToFileURL(resolve(modulePath)).href
    : modulePath;
  const mod = (await import(specifier)) as Record<string, unknown>;
  const target = mod[exportName] ?? (mod.default as Record<string, unknown> | undefined)?.[exportName];
  if (typeof target !== 'function') {
    throw new Error(`--agent target ${JSON.stringify(spec)} is not callable`);
  }
  return target as () => (text: string) => Promise<string>;
}

function printHermesStub(): void {
  console.log(
    'The Hermes wizard (doctor / setup / test / trace / diagnose /\n' +
      'attach-number) lives in the Python CLI today. Use it from the Python SDK:\n\n' +
      '  pip install getpatter\n' +
      '  patter hermes doctor\n' +
      '  patter hermes setup\n' +
      '  patter hermes test\n\n' +
      'The HermesLLM provider itself is fully available in this TypeScript SDK\n' +
      "(import { HermesLLM } from 'getpatter'). See\n" +
      'https://docs.getpatter.com/integrations/hermes for docs.',
  );
}

function printOpenClawStub(): void {
  console.log(
    'The OpenClaw wizard (doctor / setup / test / call / agents /\n' +
      'attach-number) lives in the Python CLI today. Use it from the Python SDK:\n\n' +
      '  pip install getpatter\n' +
      '  patter openclaw setup --enable-openclaw --agent receptionist  # inbound\n' +
      '  patter openclaw setup --mode outbound --agent sales           # outbound\n' +
      '  patter openclaw doctor\n\n' +
      'The OpenClawLLM provider itself is fully available in this TypeScript SDK\n' +
      "(import { OpenClawLLM } from 'getpatter'). See\n" +
      'https://docs.getpatter.com/integrations/openclaw for docs.',
  );
}

async function main(): Promise<void> {
  const command = process.argv[2];

  // Telemetry control command — never emits telemetry itself (disabling must not
  // phone home on the very invocation that opts the user out).
  if (command === 'telemetry') {
    process.exit(runTelemetryCommand(process.argv[3]));
  }

  if (command === 'eval') {
    await emitCliCommand('eval');
    try {
      process.exit(await runEvalCommand(process.argv.slice(3)));
    } catch (err) {
      console.error(`eval failed: ${err instanceof Error ? err.message : String(err)}`);
      process.exit(1);
    }
  }
  if (command === 'hermes') {
    await emitCliCommand('hermes');
    printHermesStub();
    process.exit(0);
  }
  if (command === 'openclaw') {
    await emitCliCommand('openclaw');
    printOpenClawStub();
    process.exit(0);
  }
  if (command !== 'dashboard') {
    await emitCliCommand(command ? 'other' : 'none');
    console.log('Usage: getpatter dashboard [--port 8000]');
    console.log('       getpatter eval run <suite> [--agent module:export] [--output report.json]');
    console.log('       getpatter hermes        (stub — use Python SDK for the wizard)');
    console.log('       getpatter openclaw      (stub — use Python SDK for the wizard)');
    console.log('       getpatter telemetry [status|disable|enable]');
    process.exit(command ? 1 : 0);
  }

  await emitCliCommand('dashboard');

  const { port } = parseArgs(process.argv);

  showBanner();

  const store = new MetricsStore();

  console.log(`  Dashboard:  http://localhost:${port}/`);
  console.log(`  API:        http://localhost:${port}/api/v1/calls`);
  console.log();
  console.log('  Waiting for calls…  Press Ctrl+C to stop.\n');

  const app = express();
  // Long calls carry multi-hundred-entry transcripts + per-turn metrics in
  // the call-end ingest; express's default 100 kB cap rejected them with a
  // 413 and the call silently vanished from the standalone dashboard.
  app.use(express.json({ limit: '5mb' }));

  mountDashboard(app, store);
  mountApi(app, store);

  app.get('/health', (_req, res) => {
    res.json({ status: 'ok', mode: 'dashboard' });
  });

  // Ingest endpoint — SDK POSTs call lifecycle events here so a
  // standalone dashboard surfaces them live. Three event kinds:
  //   * status="initiated" — outbound dial handed off to carrier,
  //     callee hasn't picked up yet. Surfaces the row immediately so
  //     the user sees the attempt during ringing.
  //   * default (no status) — call_start, media stream began.
  //   * ended_at present — call_end, final metrics + transcript.
  app.post('/api/dashboard/ingest', (req, res) => {
    const data = req.body as Record<string, unknown>;
    const callId = (data.call_id as string) || '';
    if (!callId) {
      res.json({ ok: false, error: 'missing call_id' });
      return;
    }
    const status = data.status as string | undefined;
    if (status === 'initiated') {
      store.recordCallInitiated(data);
      res.json({ ok: true, call_id: callId, event: 'initiated' });
      return;
    }
    if (data.ended_at) {
      // Finished-call ingest: do NOT replay it as a fresh call_start — that
      // published a spurious live event and stamped started_at = ingest-time
      // (rows rendered with start ≈ end). Mirrors the Python CLI.
      store.recordCallEnd(data, (data.metrics as Record<string, unknown>) ?? null);
      res.json({ ok: true, call_id: callId, event: 'ended' });
      return;
    }
    store.recordCallStart(data);
    res.json({ ok: true, call_id: callId });
  });

  const server = createServer(app);

  // Track open connections so we can destroy them on shutdown
  const connections = new Set<import('node:net').Socket>();
  server.on('connection', (conn) => {
    connections.add(conn);
    conn.on('close', () => connections.delete(conn));
  });

  server.listen(port, '127.0.0.1', () => {
    getLogger().info(`Dashboard server listening on port ${port}`);
  });

  const shutdown = () => {
    console.log('\nShutting down dashboard...');
    // Destroy all open connections (including SSE keep-alive)
    for (const conn of connections) conn.destroy();
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 1000);
  };
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
}

main().catch((err) => {
  console.error('Failed to start dashboard:', err);
  process.exit(1);
});
