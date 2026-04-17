/**
 * Dashboard and B2B API routes for the EmbeddedServer (Express).
 *
 * Mounts:
 *   GET /                               - HTML UI
 *   GET /api/dashboard/calls            - call list JSON
 *   GET /api/dashboard/calls/:callId    - single call JSON
 *   GET /api/dashboard/active           - active calls JSON
 *   GET /api/dashboard/aggregates       - aggregate stats JSON
 *   GET /api/dashboard/events           - SSE event stream
 *   GET /api/dashboard/export/calls     - CSV/JSON export
 *
 *   GET /api/v1/calls                   - B2B paginated call history
 *   GET /api/v1/calls/active            - B2B active calls
 *   GET /api/v1/calls/:callId           - B2B single call detail
 *   GET /api/v1/analytics/overview      - B2B aggregate stats
 *   GET /api/v1/analytics/costs         - B2B cost breakdown
 */

import type { Express } from 'express';
import { makeAuthMiddleware } from './auth';
import { callsToCsv, callsToJson } from './export';
import { DASHBOARD_HTML } from './ui';
import type { MetricsStore, SSEEvent } from './store';

export function mountDashboard(app: Express, store: MetricsStore, token = ''): void {
  const auth = makeAuthMiddleware(token);

  // --- Dashboard UI ---

  app.get('/', auth, (_req, res) => {
    res.type('text/html').send(DASHBOARD_HTML);
  });

  // --- Dashboard API ---

  app.get('/api/dashboard/calls', auth, (req, res) => {
    const limit = Math.min(parseInt((req.query.limit as string) || '50', 10) || 50, 1000);
    const offset = parseInt((req.query.offset as string) || '0', 10) || 0;
    res.json(store.getCalls(limit, offset));
  });

  app.get('/api/dashboard/calls/:callId', auth, (req, res) => {
    const call = store.getCall(String(req.params.callId));
    if (!call) {
      res.status(404).json({ error: 'Not found' });
      return;
    }
    res.json(call);
  });

  app.get('/api/dashboard/active', auth, (_req, res) => {
    res.json(store.getActiveCalls());
  });

  app.get('/api/dashboard/aggregates', auth, (_req, res) => {
    res.json(store.getAggregates());
  });

  // --- SSE endpoint ---

  app.get('/api/dashboard/events', auth, (req, res) => {
    res.writeHead(200, {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache',
      'Connection': 'keep-alive',
    });

    const listener = (event: SSEEvent) => {
      const data = JSON.stringify(event.data);
      const safeType = String(event.type ?? 'message').replace(/[\r\n]/g, '');
      res.write(`event: ${safeType}\ndata: ${data}\n\n`);
    };

    store.on('sse', listener);

    // Keepalive every 30s
    const keepalive = setInterval(() => {
      res.write(': keepalive\n\n');
    }, 30000);

    req.on('close', () => {
      clearInterval(keepalive);
      store.off('sse', listener);
    });
  });

  // --- Export endpoint ---

  app.get('/api/dashboard/export/calls', auth, (req, res) => {
    const fmt = (req.query.format as string) || 'json';
    const fromDate = (req.query.from as string) || '';
    const toDate = (req.query.to as string) || '';

    let fromTs = 0;
    let toTs = 0;
    if (fromDate) {
      const d = new Date(fromDate);
      if (!isNaN(d.getTime())) fromTs = d.getTime() / 1000;
    }
    if (toDate) {
      const d = new Date(toDate);
      if (!isNaN(d.getTime())) toTs = d.getTime() / 1000;
    }

    const calls = (fromTs || toTs)
      ? store.getCallsInRange(fromTs, toTs)
      : store.getCalls(10000);

    if (fmt === 'csv') {
      const csvData = callsToCsv(calls);
      res.setHeader('Content-Type', 'text/csv');
      res.setHeader('Content-Disposition', 'attachment; filename=patter_calls.csv');
      res.send(csvData);
    } else {
      const jsonData = callsToJson(calls);
      res.setHeader('Content-Type', 'application/json');
      res.setHeader('Content-Disposition', 'attachment; filename=patter_calls.json');
      res.send(jsonData);
    }
  });
}

export function mountApi(app: Express, store: MetricsStore, token = ''): void {
  const auth = makeAuthMiddleware(token);

  app.get('/api/v1/calls', auth, (req, res) => {
    const limit = Math.min(parseInt((req.query.limit as string) || '50', 10) || 50, 1000);
    const offset = parseInt((req.query.offset as string) || '0', 10) || 0;
    const calls = store.getCalls(limit, offset);
    res.json({
      data: calls,
      pagination: {
        limit,
        offset,
        count: calls.length,
        total: store.callCount,
      },
    });
  });

  app.get('/api/v1/calls/active', auth, (_req, res) => {
    const active = store.getActiveCalls();
    res.json({ data: active, count: active.length });
  });

  app.get('/api/v1/calls/:callId', auth, (req, res) => {
    const call = store.getCall(String(req.params.callId));
    if (!call) {
      res.status(404).json({ error: 'Call not found' });
      return;
    }
    res.json({ data: call });
  });

  app.get('/api/v1/analytics/overview', auth, (_req, res) => {
    res.json({ data: store.getAggregates() });
  });

  app.get('/api/v1/analytics/costs', auth, (req, res) => {
    const fromDate = (req.query.from as string) || '';
    const toDate = (req.query.to as string) || '';

    let fromTs = 0;
    let toTs = 0;
    if (fromDate) {
      const d = new Date(fromDate);
      if (!isNaN(d.getTime())) fromTs = d.getTime() / 1000;
    }
    if (toDate) {
      const d = new Date(toDate);
      if (!isNaN(d.getTime())) toTs = d.getTime() / 1000;
    }

    const calls = (fromTs || toTs)
      ? store.getCallsInRange(fromTs, toTs)
      : store.getCalls(10000);

    let totalCost = 0;
    let costStt = 0;
    let costTts = 0;
    let costLlm = 0;
    let costTelephony = 0;
    let callsWithCost = 0;

    for (const call of calls) {
      const m = call.metrics as Record<string, unknown> | null;
      if (!m) continue;
      const cost = (m.cost as Record<string, number>) || {};
      totalCost += cost.total || 0;
      costStt += cost.stt || 0;
      costTts += cost.tts || 0;
      costLlm += cost.llm || 0;
      costTelephony += cost.telephony || 0;
      callsWithCost++;
    }

    res.json({
      data: {
        total_cost: Math.round(totalCost * 1e6) / 1e6,
        breakdown: {
          stt: Math.round(costStt * 1e6) / 1e6,
          tts: Math.round(costTts * 1e6) / 1e6,
          llm: Math.round(costLlm * 1e6) / 1e6,
          telephony: Math.round(costTelephony * 1e6) / 1e6,
        },
        calls_analyzed: callsWithCost,
        period: {
          from: fromDate || null,
          to: toDate || null,
        },
      },
    });
  });
}
