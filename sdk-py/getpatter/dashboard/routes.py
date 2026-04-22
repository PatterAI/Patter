"""Dashboard API and UI routes for the EmbeddedServer."""

import asyncio
import json
import re
import time
from datetime import datetime

from getpatter.dashboard.store import MetricsStore


def mount_dashboard(app, store: MetricsStore, token: str = "") -> None:
    """Add dashboard routes to an existing FastAPI app.

    Mounts:
      - ``GET /`` — the web UI
      - ``GET /api/dashboard/calls`` — call list JSON
      - ``GET /api/dashboard/calls/{call_id}`` — single call JSON
      - ``GET /api/dashboard/active`` — active calls JSON
      - ``GET /api/dashboard/aggregates`` — aggregate stats JSON
      - ``GET /api/dashboard/events`` — SSE event stream
      - ``GET /api/dashboard/export/calls`` — CSV/JSON export

    Args:
        app: The FastAPI application instance.
        store: The MetricsStore to read from.
        token: Optional bearer token for authentication. When set, all
            dashboard routes require valid token via header or query param.
    """
    from fastapi import Depends, Request
    from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

    from getpatter.dashboard.auth import make_auth_dependency

    auth = make_auth_dependency(token=token)

    @app.get("/", response_class=HTMLResponse)
    async def dashboard_ui(_=Depends(auth)):
        from getpatter.dashboard.ui import DASHBOARD_HTML

        return HTMLResponse(content=DASHBOARD_HTML)

    @app.get("/api/dashboard/calls", dependencies=[Depends(auth)])
    async def dashboard_calls(request: Request):
        try:
            limit = min(int(request.query_params.get("limit", "50")), 1000)
        except (ValueError, TypeError):
            limit = 50
        try:
            offset = max(int(request.query_params.get("offset", "0")), 0)
        except (ValueError, TypeError):
            offset = 0
        return JSONResponse(content=store.get_calls(limit=limit, offset=offset))

    @app.get("/api/dashboard/calls/{call_id}")
    async def dashboard_call_detail(call_id: str, _=Depends(auth)):
        call = store.get_call(call_id)
        if call is None:
            return JSONResponse(content={"error": "Not found"}, status_code=404)
        return JSONResponse(content=call)

    @app.get("/api/dashboard/active")
    async def dashboard_active(_=Depends(auth)):
        return JSONResponse(content=store.get_active_calls())

    @app.get("/api/dashboard/aggregates")
    async def dashboard_aggregates(_=Depends(auth)):
        return JSONResponse(content=store.get_aggregates())

    # --- SSE endpoint ---

    @app.get("/api/dashboard/events")
    async def dashboard_sse(_=Depends(auth)):
        queue = store.subscribe()

        async def event_generator():
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=30.0)
                        event_type = event.get("type", "message")
                        event_type = re.sub(r'[\r\n]', '', event_type)
                        data = json.dumps(event.get("data", {}), default=str)
                        yield f"event: {event_type}\ndata: {data}\n\n"
                    except asyncio.TimeoutError:
                        # Send keepalive
                        yield ": keepalive\n\n"
            except asyncio.CancelledError:
                pass
            finally:
                store.unsubscribe(queue)

        return StreamingResponse(
            event_generator(), media_type="text/event-stream"
        )

    # --- Export endpoint ---

    @app.get("/api/dashboard/export/calls", dependencies=[Depends(auth)])
    async def dashboard_export_calls(request: Request):
        fmt = request.query_params.get("format", "json")
        from_date = request.query_params.get("from", "")
        to_date = request.query_params.get("to", "")

        from_ts = 0.0
        to_ts = 0.0
        if from_date:
            try:
                from_ts = datetime.fromisoformat(from_date).timestamp()
            except ValueError:
                pass
        if to_date:
            try:
                to_ts = datetime.fromisoformat(to_date).timestamp()
            except ValueError:
                pass

        if from_ts or to_ts:
            calls = store.get_calls_in_range(from_ts=from_ts, to_ts=to_ts)
        else:
            calls = store.get_calls(limit=10000)

        if fmt == "csv":
            from getpatter.dashboard.export import calls_to_csv

            csv_data = calls_to_csv(calls)
            return StreamingResponse(
                iter([csv_data]),
                media_type="text/csv",
                headers={
                    "Content-Disposition": "attachment; filename=patter_calls.csv"
                },
            )
        else:
            from getpatter.dashboard.export import calls_to_json

            json_data = calls_to_json(calls)
            return StreamingResponse(
                iter([json_data]),
                media_type="application/json",
                headers={
                    "Content-Disposition": "attachment; filename=patter_calls.json"
                },
            )
