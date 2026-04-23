"""Embedded HTTP/WebSocket server for local mode."""

from __future__ import annotations

import asyncio
import base64
import logging
import signal
import time

from fastapi import FastAPI, Request, Response, WebSocket

from getpatter.local_config import LocalConfig
from getpatter.models import Agent
from getpatter.utils.log_sanitize import sanitize_log_value

logger = logging.getLogger("patter")


def _validate_telnyx_signature(
    raw_body: bytes,
    signature: str,
    timestamp: str,
    public_key: str,
    tolerance_sec: int = 300,
) -> bool:
    """Verify a Telnyx webhook Ed25519 signature.

    Signed payload is ``timestamp + "|" + raw_body``. Returns False when any
    step fails (missing deps, bad base64, stale timestamp, bad signature).
    """
    if not signature or not timestamp or not public_key:
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    now_ms = int(time.time() * 1000)
    age_ms = now_ms - ts
    if age_ms < 0 or age_ms > tolerance_sec * 1000:
        return False
    try:
        from cryptography.hazmat.primitives.serialization import load_der_public_key
        from cryptography.exceptions import InvalidSignature
    except ImportError:
        logger.warning(
            "cryptography package not installed — cannot verify Telnyx signature. "
            "Install with: pip install cryptography"
        )
        return False
    try:
        key_bytes = base64.b64decode(public_key)
        key = load_der_public_key(key_bytes)
        payload = timestamp.encode("utf-8") + b"|" + raw_body
    except (ValueError, TypeError):
        return False
    except Exception:
        return False
    # The telnyx-signature-ed25519 header may contain multiple
    # comma-separated signatures during key rotation.  Accept the webhook
    # if any one of them verifies.  Fail-closed when none match.
    for raw_sig in signature.split(","):
        raw_sig = raw_sig.strip()
        if not raw_sig:
            continue
        try:
            sig_bytes = base64.b64decode(raw_sig)
            key.verify(sig_bytes, payload)
            return True
        except (InvalidSignature, ValueError, TypeError):
            continue
        except Exception:
            continue
    return False


class EmbeddedServer:
    """Self-contained server that handles Twilio/Telnyx webhooks and streams.

    Usage::

        server = EmbeddedServer(config=local_cfg, agent=my_agent)
        server.on_call_start = my_start_handler
        await server.start(port=8000)
    """

    def __init__(
        self,
        config: LocalConfig,
        agent: Agent,
        recording: bool = False,
        voicemail_message: str = "",
        pricing: dict | None = None,
        dashboard: bool = True,
        dashboard_token: str = "",
    ) -> None:
        self.config = config
        self.agent = agent
        self.recording = recording
        self.voicemail_message = voicemail_message
        self.pricing = pricing
        self.dashboard = dashboard
        self.dashboard_token = dashboard_token
        self._server = None
        self._app = None
        self._active_connections: set[WebSocket] = set()
        self._shutting_down = False
        self.on_call_start = None
        self.on_call_end = None
        self.on_transcript = None
        self.on_message = None
        self.on_metrics = None
        self._telnyx_sig_warning_logged = False
        self._metrics_store = None

    def _wrap_callbacks(self):
        """Return (on_call_start, on_call_end, on_metrics) wrappers.

        Each wrapper feeds data into the dashboard store first, then calls
        the user-provided callback (if any).  Completed calls are also
        persisted to ``~/.patter/data/calls.jsonl`` and pushed to any
        running standalone dashboard.
        """
        store = self._metrics_store
        user_start = self.on_call_start
        user_end = self.on_call_end
        user_metrics = self.on_metrics

        async def _on_call_start(data):
            if store is not None:
                store.record_call_start(data)
            # Notify standalone dashboard so active calls appear immediately
            try:
                from getpatter.dashboard.persistence import notify_dashboard
                notify_dashboard(data)
            except Exception:
                pass
            if user_start is not None:
                await user_start(data)

        async def _on_call_end(data):
            if store is not None:
                store.record_call_end(data, metrics=data.get("metrics"))
            # Notify standalone dashboard (if running)
            try:
                from getpatter.dashboard.persistence import notify_dashboard
                notify_dashboard(data)
            except Exception:
                pass
            if user_end is not None:
                await user_end(data)

        async def _on_metrics(data):
            if store is not None:
                store.record_turn(data)
            if user_metrics is not None:
                await user_metrics(data)

        return _on_call_start, _on_call_end, _on_metrics

    def _create_app(self):
        """Build the FastAPI application with webhook + stream routes."""
        from getpatter.handlers.twilio_handler import (
            twilio_webhook_handler,
            twilio_stream_bridge,
        )
        from getpatter.handlers.telnyx_handler import telnyx_stream_bridge

        app = FastAPI(title="Patter Local Server")

        # --- Dashboard ---
        if self.dashboard:
            from getpatter.dashboard.routes import mount_dashboard

            from getpatter.dashboard.store import MetricsStore
            self._metrics_store = MetricsStore()

            mount_dashboard(app, self._metrics_store, token=self.dashboard_token)

            from getpatter.api_routes import mount_api
            mount_api(app, self._metrics_store, token=self.dashboard_token)

        @app.get("/health")
        async def health():
            return {"status": "ok", "mode": "local"}

        # --- Twilio ---

        async def _read_and_validate_twilio_form(request: Request):
            """Read the form body and verify the X-Twilio-Signature header.

            Returns the parsed form on success, or a 403 Response when the
            signature is present but invalid.  When no auth token is
            configured, or the `twilio` package is missing, the body is
            returned without validation (logged once).
            """
            if self.config.twilio_token:
                try:
                    from twilio.request_validator import RequestValidator
                    form_data = await request.form()
                    validator = RequestValidator(self.config.twilio_token)
                    url = str(request.url).replace("http://", "https://")
                    signature = request.headers.get("X-Twilio-Signature", "")
                    if not validator.validate(url, dict(form_data), signature):
                        return Response(status_code=403, content="Invalid signature")
                    return form_data
                except ImportError:
                    logger.warning("twilio package not installed; skipping signature validation")
                    return await request.form()
            return await request.form()

        @app.post("/webhooks/twilio/voice")
        async def twilio_voice(request: Request):
            form_or_response = await _read_and_validate_twilio_form(request)
            if isinstance(form_or_response, Response):
                return form_or_response
            form_data = form_or_response
            call_sid = form_data.get("CallSid", "")
            # Twilio sends both `From` and `Caller` — `From` is set for direct
            # inbound dials, `Caller` is what Twilio sees on the SIP trunk and
            # is the reliable fallback when the number is anonymised or
            # masked. Same for `To` / `Called`.
            caller = form_data.get("From", "") or form_data.get("Caller", "")
            callee = form_data.get("To", "") or form_data.get("Called", "")
            twiml = twilio_webhook_handler(
                call_sid, caller, callee, self.config.webhook_url
            )
            return Response(content=twiml, media_type="text/xml")

        # Twilio posts here for every status transition of a call
        # (initiated → ringing → in-progress → completed | no-answer |
        # busy | failed | canceled). Keeps the dashboard honest even when
        # the call never reaches the media channel. See BUG #06.
        @app.post("/webhooks/twilio/status")
        async def twilio_status_callback(request: Request):
            form_or_response = await _read_and_validate_twilio_form(request)
            if isinstance(form_or_response, Response):
                return form_or_response
            form = form_or_response
            call_sid = form.get("CallSid", "")
            call_status = form.get("CallStatus", "")
            duration = form.get("CallDuration", "") or form.get("Duration", "")
            logger.info(
                "Twilio status %s for call %s (duration=%s)",
                sanitize_log_value(call_status),
                sanitize_log_value(call_sid),
                sanitize_log_value(duration),
            )
            if self._metrics_store is not None and call_sid and call_status:
                extra: dict = {}
                if duration:
                    try:
                        extra["duration_seconds"] = float(duration)
                    except ValueError:
                        pass
                self._metrics_store.update_call_status(call_sid, call_status, **extra)
            return Response(content="", status_code=204)

        @app.post("/webhooks/twilio/recording")
        async def twilio_recording_callback(request: Request):
            form_or_response = await _read_and_validate_twilio_form(request)
            if isinstance(form_or_response, Response):
                return form_or_response
            form = form_or_response
            recording_sid = form.get("RecordingSid", "")
            recording_url = form.get("RecordingUrl", "")
            call_sid = form.get("CallSid", "")
            logger.info(
                "Recording %s for call %s: %s",
                sanitize_log_value(recording_sid),
                sanitize_log_value(call_sid),
                sanitize_log_value(recording_url),
            )
            return Response(content="", status_code=204)

        @app.post("/webhooks/twilio/amd")
        async def twilio_amd_callback(request: Request):
            form_or_response = await _read_and_validate_twilio_form(request)
            if isinstance(form_or_response, Response):
                return form_or_response
            form = form_or_response
            answered_by = form.get("AnsweredBy", "")
            call_sid = form.get("CallSid", "")
            logger.info("AMD result for %s: %s", call_sid, answered_by)

            if (
                answered_by in ("machine_end_beep", "machine_end_silence")
                and self.voicemail_message
                and self.config.twilio_sid
                and self.config.twilio_token
            ):
                from getpatter.handlers.twilio_handler import _xml_escape, _validate_twilio_sid

                if not _validate_twilio_sid(call_sid, "CA"):
                    logger.warning("AMD callback: invalid CallSid format %r, ignoring", call_sid)
                    return Response(content="", status_code=204)

                import httpx as _httpx

                twiml = f"<Response><Say>{_xml_escape(self.voicemail_message)}</Say><Hangup/></Response>"
                try:
                    async with _httpx.AsyncClient() as _http:
                        await _http.post(
                            f"https://api.twilio.com/2010-04-01/Accounts/{self.config.twilio_sid}/Calls/{call_sid}.json",
                            auth=(self.config.twilio_sid, self.config.twilio_token),
                            data={"Twiml": twiml},
                        )
                    logger.info("Voicemail dropped for %s", call_sid)
                except Exception as exc:
                    logger.warning("Could not drop voicemail: %s", exc)

            return Response(content="", status_code=204)

        @app.websocket("/ws/stream/{call_id}")
        async def twilio_stream_handler(websocket: WebSocket, call_id: str):
            self._active_connections.add(websocket)
            try:
                _start, _end, _metrics = self._wrap_callbacks()
                await twilio_stream_bridge(
                    websocket=websocket,
                    agent=self.agent,
                    openai_key=self.config.openai_key,
                    on_call_start=_start,
                    on_call_end=_end,
                    on_transcript=self.on_transcript,
                    on_message=self.on_message,
                    deepgram_key=self.config.deepgram_key,
                    elevenlabs_key=self.config.elevenlabs_key,
                    twilio_sid=self.config.twilio_sid,
                    twilio_token=self.config.twilio_token,
                    recording=self.recording,
                    on_metrics=_metrics,
                    pricing=self.pricing,
                )
            finally:
                self._active_connections.discard(websocket)

        # --- Telnyx ---

        @app.post("/webhooks/telnyx/voice")
        async def telnyx_voice(request: Request):
            raw_body = await request.body()
            telnyx_public_key = getattr(self.config, "telnyx_public_key", "")
            if telnyx_public_key:
                signature = request.headers.get("telnyx-signature-ed25519", "")
                timestamp = request.headers.get("telnyx-timestamp", "")
                if not _validate_telnyx_signature(raw_body, signature, timestamp, telnyx_public_key):
                    logger.warning("Telnyx webhook rejected: invalid or missing Ed25519 signature")
                    return Response(status_code=403, content="Invalid signature")
            elif not self._telnyx_sig_warning_logged:
                self._telnyx_sig_warning_logged = True
                logger.warning(
                    "Telnyx webhook signature verification is disabled. "
                    "Set telnyx_public_key in LocalConfig for production use."
                )
            import json as _json
            try:
                body = _json.loads(raw_body)
            except (ValueError, TypeError):
                return Response(status_code=400, content="Invalid JSON body")
            if not isinstance(body.get("data"), dict) or not isinstance(body.get("data", {}).get("payload"), dict):
                logger.warning("Telnyx webhook rejected: missing data.payload structure.")
                return Response(status_code=400, content="Invalid webhook structure")
            data = body["data"]
            event_type = data.get("event_type", "")
            payload = data["payload"]
            call_control_id = payload.get("call_control_id", "")
            caller = payload.get("from", "")
            callee = payload.get("to", "")
            if not call_control_id:
                logger.warning("Telnyx webhook rejected: missing call_control_id.")
                return Response(status_code=400, content="Invalid webhook payload")

            # Telnyx Call Control is a REST API — the webhook body is a
            # notification, not a command transport. We react by POSTing
            # actions/answer and actions/streaming_start to the Call Control
            # REST endpoint. See BUG #16.
            api_key = self.config.telnyx_key
            if not api_key:
                logger.warning("Telnyx webhook: missing telnyx_key in LocalConfig")
                return Response(status_code=500, content="Missing Telnyx API key")

            import httpx as _httpx
            api_base = "https://api.telnyx.com/v2"
            auth_headers = {"Authorization": f"Bearer {api_key}"}

            try:
                if event_type == "call.initiated":
                    logger.info("Telnyx call.initiated %s — answering", call_control_id)
                    async with _httpx.AsyncClient(timeout=10.0) as client:
                        resp = await client.post(
                            f"{api_base}/calls/{call_control_id}/actions/answer",
                            headers=auth_headers,
                            json={},
                        )
                        if resp.status_code >= 400:
                            logger.warning("Telnyx answer failed: %s %s", resp.status_code, resp.text)
                elif event_type == "call.answered":
                    from urllib.parse import quote as _quote
                    stream_url = (
                        f"wss://{self.config.webhook_url}/ws/telnyx/stream/{call_control_id}"
                        f"?caller={_quote(caller)}&callee={_quote(callee)}"
                    )
                    logger.info("Telnyx call.answered %s — starting stream", call_control_id)
                    async with _httpx.AsyncClient(timeout=10.0) as client:
                        resp = await client.post(
                            f"{api_base}/calls/{call_control_id}/actions/streaming_start",
                            headers=auth_headers,
                            json={
                                "stream_url": stream_url,
                                "stream_track": "both_tracks",
                                "stream_bidirectional_mode": "rtp",
                                "stream_bidirectional_codec": "PCMU",
                                "stream_bidirectional_sampling_rate": 8000,
                                "stream_bidirectional_target_legs": "self",
                            },
                        )
                        if resp.status_code >= 400:
                            logger.warning("Telnyx streaming_start failed: %s %s", resp.status_code, resp.text)
                else:
                    logger.debug("Telnyx event ignored: %s", event_type)
            except Exception as exc:
                logger.exception("Telnyx webhook handler error: %s", exc)

            return Response(status_code=200)

        @app.websocket("/ws/telnyx/stream/{call_id}")
        async def telnyx_stream_handler(websocket: WebSocket, call_id: str):
            self._active_connections.add(websocket)
            try:
                _start, _end, _metrics = self._wrap_callbacks()
                await telnyx_stream_bridge(
                    websocket=websocket,
                    agent=self.agent,
                    openai_key=self.config.openai_key,
                    on_call_start=_start,
                    on_call_end=_end,
                    on_transcript=self.on_transcript,
                    on_message=self.on_message,
                    deepgram_key=self.config.deepgram_key,
                    elevenlabs_key=self.config.elevenlabs_key,
                    telnyx_key=self.config.telnyx_key,
                    recording=self.recording,
                    on_metrics=_metrics,
                    pricing=self.pricing,
                )
            finally:
                self._active_connections.discard(websocket)

        self._app = app
        return app

    async def start(self, port: int = 8000) -> None:
        """Start the embedded server.

        Optionally auto-configures the Twilio webhook URL if credentials are
        present in ``LocalConfig``.

        Args:
            port: Local TCP port to bind to (default 8000).
        """
        import uvicorn

        app = self._create_app()

        # Auto-configure Twilio webhook URL if possible
        if (
            self.config.telephony_provider == "twilio"
            and self.config.twilio_sid
            and self.config.webhook_url
        ):
            try:
                from getpatter.providers.twilio_adapter import TwilioAdapter  # type: ignore[import]

                adapter = TwilioAdapter(
                    account_sid=self.config.twilio_sid,
                    auth_token=self.config.twilio_token,
                )
                webhook_url = (
                    f"https://{self.config.webhook_url}/webhooks/twilio/voice"
                )
                await adapter.configure_number(self.config.phone_number, webhook_url)
                logger.info("Twilio webhook set to %s", webhook_url)
            except Exception as exc:
                logger.warning("Could not auto-configure webhook: %s", exc)
                logger.info(
                    "Set webhook manually to: https://%s/webhooks/twilio/voice",
                    self.config.webhook_url,
                )

        logger.info("Server starting on port %s", port)
        logger.info("Webhook URL: https://%s", self.config.webhook_url)
        logger.info("Phone:   %s", self.config.phone_number)
        logger.info("Agent:   %s / %s", self.agent.model, self.agent.voice)
        if self.dashboard:
            print("\n──── Dashboard ─────────────────────────────────────")
            logger.info("URL: http://127.0.0.1:%s/", port)
            if not self.dashboard_token:
                logger.warning(
                    "Dashboard is enabled without authentication. "
                    "Set dashboard_token to protect call data. "
                    "This is safe for local development but should "
                    "not be exposed on a public network."
                )
            print("────────────────────────────────────────────────────\n")

        # Suppress Uvicorn's "Uvicorn running on..." startup message
        # but keep request logs (INFO level) visible
        logging.getLogger("uvicorn.error").setLevel(logging.WARNING)

        config = uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="info"
        )
        self._server = uvicorn.Server(config)

        # Register signal handlers for graceful shutdown
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self.stop()))

        await self._server.serve()

    async def stop(self) -> None:
        """Gracefully stop the embedded server.

        Closes all active WebSocket connections, waits up to 10 seconds
        for in-progress calls to finish, then shuts down the uvicorn server.
        """
        if self._shutting_down:
            return
        self._shutting_down = True

        logger.info("Graceful shutdown initiated — closing %d active connection(s)", len(self._active_connections))

        # Signal all active WebSocket connections to close
        for ws in list(self._active_connections):
            try:
                await ws.close(code=1001, reason="Server shutting down")
            except Exception:
                pass

        # Wait up to 10 seconds for active connections to drain
        for _ in range(100):
            if not self._active_connections:
                break
            await asyncio.sleep(0.1)

        if self._active_connections:
            logger.warning(
                "Shutdown timeout — %d connection(s) still active, forcing close",
                len(self._active_connections),
            )

        # Shutdown the uvicorn server
        if self._server:
            self._server.should_exit = True
