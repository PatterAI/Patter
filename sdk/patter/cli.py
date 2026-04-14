"""Patter CLI — standalone dashboard and utilities."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys


def main() -> None:
    """Entry point for the ``patter`` command."""
    parser = argparse.ArgumentParser(
        prog="patter",
        description="Patter CLI — Voice AI platform utilities",
    )
    subparsers = parser.add_subparsers(dest="command")

    dash = subparsers.add_parser(
        "dashboard",
        help="Start the standalone call monitoring dashboard",
    )
    dash.add_argument(
        "--port", type=int, default=8000, help="Port to serve dashboard on (default: 8000)"
    )

    args = parser.parse_args()

    if args.command == "dashboard":
        asyncio.run(_run_dashboard(args.port))
    else:
        parser.print_help()
        sys.exit(1)


async def _run_dashboard(port: int) -> None:
    """Start the standalone dashboard server."""
    try:
        from fastapi import FastAPI, Request
        import uvicorn
    except ImportError:
        print(
            "The dashboard requires FastAPI and Uvicorn.\n"
            "Install with:  pip install patter[local]"
        )
        sys.exit(1)

    from patter.banner import show_banner
    from patter.dashboard.store import MetricsStore
    from patter.dashboard.routes import mount_dashboard
    from patter.api_routes import mount_api

    show_banner()

    store = MetricsStore()

    print(f"  Dashboard:  http://localhost:{port}/")
    print(f"  API:        http://localhost:{port}/api/v1/calls")
    print()
    print("  Waiting for calls…  Press Ctrl+C to stop.\n")

    app = FastAPI(title="Patter Dashboard")
    mount_dashboard(app, store)
    mount_api(app, store)

    @app.get("/health")
    async def health():
        return {"status": "ok", "mode": "dashboard"}

    # Ingest endpoint — SDK POSTs completed call data here for live updates
    @app.post("/api/dashboard/ingest")
    async def ingest(request: Request):
        data = await request.json()
        call_id = data.get("call_id", "")
        if not call_id:
            return {"ok": False, "error": "missing call_id"}
        store.record_call_start(data)
        if data.get("ended_at"):
            store.record_call_end(data, metrics=data.get("metrics"))
        return {"ok": True, "call_id": call_id}

    # Suppress Uvicorn's startup banner (we have our own)
    logging.getLogger("uvicorn.error").setLevel(logging.WARNING)

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    main()
