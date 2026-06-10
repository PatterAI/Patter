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
        description="Patter CLI — Give your AI agent a phone number",
    )
    subparsers = parser.add_subparsers(dest="command")

    dash = subparsers.add_parser(
        "dashboard",
        help="Start the standalone call monitoring dashboard",
    )
    dash.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to serve dashboard on (default: 8000)",
    )

    # patter eval run <suite>
    from getpatter.evals.cli import build_eval_parser, dispatch_eval

    build_eval_parser(subparsers)

    # patter hermes {doctor|setup|test|trace|diagnose|attach-number|numbers}
    from getpatter.cli_hermes import build_hermes_parser, dispatch_hermes

    build_hermes_parser(subparsers)

    # patter openclaw {doctor|setup|test|call|agents|attach-number|numbers}
    from getpatter.cli_openclaw import build_openclaw_parser, dispatch_openclaw

    build_openclaw_parser(subparsers)

    # patter telemetry [status|disable|enable]
    tel = subparsers.add_parser(
        "telemetry",
        help="Manage anonymous usage telemetry (status / disable / enable)",
    )
    tel.add_argument(
        "action",
        nargs="?",
        choices=["status", "disable", "enable"],
        default="status",
        help="status (default), disable, or enable",
    )

    args = parser.parse_args()

    # The telemetry control command never emits telemetry itself — disabling must
    # not phone home on the very invocation that opts the user out.
    if args.command == "telemetry":
        sys.exit(_run_telemetry_command(args.action))

    _emit_cli_command(args.command)

    if args.command == "dashboard":
        asyncio.run(_run_dashboard(args.port))
    elif args.command == "eval":
        sys.exit(dispatch_eval(args))
    elif args.command == "hermes":
        sys.exit(dispatch_hermes(args))
    elif args.command == "openclaw":
        sys.exit(dispatch_openclaw(args))
    else:
        parser.print_help()
        sys.exit(1)


def _emit_cli_command(command: str | None) -> None:
    """Record which CLI command was invoked (the name only — never args/flags).

    Builds a standalone telemetry client; the buffered event ships via the
    process-exit flush (immediate for short commands, on Ctrl+C for the dashboard).
    Best-effort and fail-safe — never blocks or breaks the CLI.
    """
    try:
        from getpatter import __version__
        from getpatter.telemetry import TelemetryClient

        TelemetryClient(sdk_version=__version__).record(
            "cli_command", cli_command=command or "none"
        )
    except Exception:
        pass


def _run_telemetry_command(action: str) -> int:
    """Implement ``patter telemetry status|disable|enable`` (parity with
    ``next telemetry``). Persists a machine-level opt-out marker read by consent."""
    import os

    from getpatter.telemetry.client import DEFAULT_ENDPOINT
    from getpatter.telemetry.consent import is_enabled
    from getpatter.telemetry.install_id import is_opted_out, set_opt_out

    if action == "disable":
        try:
            set_opt_out(True)
        except OSError as exc:
            print(f"Could not write the opt-out marker: {exc}")
            return 1
        print("Anonymous telemetry disabled. No usage data will be sent.")
        return 0
    if action == "enable":
        try:
            set_opt_out(False)
        except OSError as exc:
            print(f"Could not remove the opt-out marker: {exc}")
            return 1
        print("Anonymous telemetry re-enabled (opt-out model, on by default).")
        return 0

    # status
    endpoint = os.getenv("PATTER_TELEMETRY_ENDPOINT") or DEFAULT_ENDPOINT
    print(
        f"Anonymous usage telemetry: {'ENABLED' if is_enabled() else 'DISABLED'}"
    )
    if is_opted_out():
        print("  Opted out via: getpatter telemetry disable (persisted marker)")
    print(f"  Endpoint: {endpoint}")
    print(
        "  Inspect what would be sent (prints, sends nothing): "
        "PATTER_TELEMETRY_DEBUG=1"
    )
    print(
        "  Disable: getpatter telemetry disable  |  DO_NOT_TRACK=1  |  "
        "PATTER_TELEMETRY_DISABLED=1"
    )
    print("  Details: https://docs.getpatter.com/telemetry")
    return 0


async def _run_dashboard(port: int) -> None:
    """Start the standalone dashboard server."""
    try:
        from fastapi import FastAPI, Request
        import uvicorn
    except ImportError:
        print(
            "The dashboard requires FastAPI and Uvicorn.\n"
            "Install with:  pip install getpatter[local]"
        )
        sys.exit(1)

    from getpatter.banner import show_banner
    from getpatter.dashboard.store import MetricsStore
    from getpatter.dashboard.routes import mount_dashboard
    from getpatter.api_routes import mount_api

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

    # Ingest endpoint — SDK POSTs call lifecycle events here so a
    # standalone dashboard surfaces them live. Three event kinds:
    #   * status="initiated" — outbound dial handed off to carrier,
    #     callee hasn't picked up yet. Surfaces the row immediately so
    #     the user sees the attempt during ringing.
    #   * default (no status) — call_start, media stream began.
    #   * ended_at present — call_end, final metrics + transcript.
    @app.post("/api/dashboard/ingest")
    async def ingest(request: Request):
        data = await request.json()
        call_id = data.get("call_id", "")
        if not call_id:
            return {"ok": False, "error": "missing call_id"}
        status = data.get("status")
        if status == "initiated":
            store.record_call_initiated(data)
            return {"ok": True, "call_id": call_id, "event": "initiated"}
        if data.get("ended_at"):
            # Finished-call ingest: do NOT replay it as a fresh call_start —
            # that published a spurious live event and stamped
            # ``started_at = ingest-time`` (rows rendered with start ≈ end).
            store.record_call_end(data, metrics=data.get("metrics"))
            return {"ok": True, "call_id": call_id, "event": "ended"}
        store.record_call_start(data)
        return {"ok": True, "call_id": call_id}

    # Suppress Uvicorn's startup banner (we have our own)
    logging.getLogger("uvicorn.error").setLevel(logging.WARNING)

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    main()
