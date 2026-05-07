/**
 * Dashboard notification for live call updates.
 *
 * When the SDK completes a call, it fires a POST to the standalone dashboard
 * (if running) so calls appear in real time.  Data lives only in memory —
 * nothing is written to disk.
 *
 * TODO(parity): Python's `notify_dashboard` is now an async fire-and-forget
 * coroutine (see libraries/python/getpatter/dashboard/persistence.py). This TS version
 * uses `http.request` which is already non-blocking, but for parity consider
 * exposing this as `async function notifyDashboard(...): Promise<void>` so
 * call sites can `await` or `void` it explicitly, matching the Python API.
 */

import http from 'node:http';

/**
 * Fire-and-forget POST a completed call payload into a locally-running dashboard, if any.
 *
 * Skip entirely when ``PATTER_DASHBOARD_NOTIFY`` is set to ``0``/``false``
 * (case-insensitive). This avoids 404 spam in the receiver's access log
 * when callers embed Patter alongside their own HTTP server on port
 * 8000 (e.g. agent-to-agent test runners).
 */
export function notifyDashboard(
  callData: Record<string, unknown>,
  port = 8000,
): void {
  const flag = (process.env.PATTER_DASHBOARD_NOTIFY ?? '').trim().toLowerCase();
  if (flag === '0' || flag === 'false' || flag === 'no' || flag === 'off') {
    return;
  }
  try {
    const body = JSON.stringify(callData);
    const req = http.request(
      {
        hostname: '127.0.0.1',
        port,
        path: '/api/dashboard/ingest',
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Content-Length': Buffer.byteLength(body),
        },
        timeout: 1000,
      },
      () => { /* ignore response */ },
    );
    req.on('error', () => { /* dashboard not running, ignore */ });
    req.write(body);
    req.end();
  } catch {
    // silently ignore
  }
}
