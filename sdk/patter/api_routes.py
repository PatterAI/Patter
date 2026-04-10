"""B2B REST API routes for programmatic access to call data and analytics."""

from datetime import datetime

from patter.dashboard.store import MetricsStore


def _parse_int(value: str, name: str, default: int, max_val: int | None = None) -> int:
    """Parse an integer query parameter, returning *default* on bad input."""
    try:
        result = int(value)
    except (ValueError, TypeError):
        result = default
    if result < 0:
        result = 0
    if max_val is not None and result > max_val:
        result = max_val
    return result


def mount_api(app, store: MetricsStore, token: str = "") -> None:
    """Add B2B API routes to an existing FastAPI app.

    Mounts:
      - ``GET /api/v1/calls``              -- paginated call history
      - ``GET /api/v1/calls/active``       -- currently active calls
      - ``GET /api/v1/calls/{call_id}``    -- single call detail
      - ``GET /api/v1/analytics/overview``  -- aggregate stats
      - ``GET /api/v1/analytics/costs``     -- cost breakdown over time

    Args:
        app: The FastAPI application instance.
        store: The MetricsStore to read from.
        token: Optional bearer token for authentication.
    """
    from fastapi import Depends, Request
    from fastapi.responses import JSONResponse

    from patter.dashboard.auth import make_auth_dependency

    auth = make_auth_dependency(token=token)

    @app.get("/api/v1/calls", dependencies=[Depends(auth)])
    async def api_list_calls(request: Request):
        limit = _parse_int(
            request.query_params.get("limit", "50"), "limit", 50, max_val=1000,
        )
        offset = _parse_int(
            request.query_params.get("offset", "0"), "offset", 0,
        )
        calls = store.get_calls(limit=limit, offset=offset)
        return JSONResponse(content={
            "data": calls,
            "pagination": {
                "limit": limit,
                "offset": offset,
                "count": len(calls),
                "total": store.call_count,
            },
        })

    # Register /calls/active BEFORE /calls/{call_id} to avoid path conflict
    @app.get("/api/v1/calls/active", dependencies=[Depends(auth)])
    async def api_active_calls():
        active = store.get_active_calls()
        return JSONResponse(content={
            "data": active,
            "count": len(active),
        })

    @app.get("/api/v1/calls/{call_id}", dependencies=[Depends(auth)])
    async def api_call_detail(call_id: str):
        call = store.get_call(call_id)
        if call is None:
            return JSONResponse(
                content={"error": "Call not found"}, status_code=404
            )
        return JSONResponse(content={"data": call})

    @app.get("/api/v1/analytics/overview", dependencies=[Depends(auth)])
    async def api_analytics_overview():
        aggregates = store.get_aggregates()
        return JSONResponse(content={"data": aggregates})

    @app.get("/api/v1/analytics/costs", dependencies=[Depends(auth)])
    async def api_analytics_costs(request: Request):
        from_ts = 0.0
        to_ts = 0.0
        from_date = request.query_params.get("from", "")
        to_date = request.query_params.get("to", "")

        if from_date:
            try:
                from_ts = datetime.fromisoformat(from_date).timestamp()
            except ValueError:
                return JSONResponse(
                    content={"error": "Invalid 'from' date format"},
                    status_code=400,
                )
        if to_date:
            try:
                to_ts = datetime.fromisoformat(to_date).timestamp()
            except ValueError:
                return JSONResponse(
                    content={"error": "Invalid 'to' date format"},
                    status_code=400,
                )

        if from_ts or to_ts:
            calls = store.get_calls_in_range(from_ts=from_ts, to_ts=to_ts)
        else:
            calls = store.get_calls(limit=10000)

        total_cost = 0.0
        cost_stt = 0.0
        cost_tts = 0.0
        cost_llm = 0.0
        cost_telephony = 0.0
        calls_with_cost = 0

        for call in calls:
            m = call.get("metrics")
            if m is None:
                continue
            cost = m.get("cost", {})
            total_cost += cost.get("total", 0.0)
            cost_stt += cost.get("stt", 0.0)
            cost_tts += cost.get("tts", 0.0)
            cost_llm += cost.get("llm", 0.0)
            cost_telephony += cost.get("telephony", 0.0)
            calls_with_cost += 1

        return JSONResponse(content={
            "data": {
                "total_cost": round(total_cost, 6),
                "breakdown": {
                    "stt": round(cost_stt, 6),
                    "tts": round(cost_tts, 6),
                    "llm": round(cost_llm, 6),
                    "telephony": round(cost_telephony, 6),
                },
                "calls_analyzed": calls_with_cost,
                "period": {
                    "from": from_date or None,
                    "to": to_date or None,
                },
            },
        })
