"""Embedded HTTP/WebSocket server for local mode."""

from __future__ import annotations

import logging

from patter.local_config import LocalConfig
from patter.models import Agent

logger = logging.getLogger("patter")


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
    ) -> None:
        self.config = config
        self.agent = agent
        self.recording = recording
        self.voicemail_message = voicemail_message
        self._server = None
        self._app = None
        self.on_call_start = None
        self.on_call_end = None
        self.on_transcript = None
        self.on_message = None
        self._telnyx_sig_warning_logged = False

    def _create_app(self):
        """Build the FastAPI application with webhook + stream routes."""
        from fastapi import FastAPI, Request, Response, WebSocket
        from patter.handlers.twilio_handler import (
            twilio_webhook_handler,
            twilio_stream_bridge,
        )
        from patter.handlers.telnyx_handler import (
            telnyx_webhook_handler,
            telnyx_stream_bridge,
        )

        app = FastAPI(title="Patter Local Server")

        @app.get("/health")
        async def health():
            return {"status": "ok", "mode": "local"}

        # --- Twilio ---

        @app.post("/webhooks/twilio/voice")
        async def twilio_voice(request: Request):
            # Validate Twilio signature
            if self.config.twilio_token:
                try:
                    from twilio.request_validator import RequestValidator
                    form_data = await request.form()
                    validator = RequestValidator(self.config.twilio_token)
                    url = str(request.url).replace("http://", "https://")
                    signature = request.headers.get("X-Twilio-Signature", "")
                    if not validator.validate(url, dict(form_data), signature):
                        return Response(status_code=403, content="Invalid signature")
                except ImportError:
                    logger.warning("twilio package not installed; skipping signature validation")
                    form_data = await request.form()
            else:
                form_data = await request.form()
            call_sid = form_data.get("CallSid", "")
            caller = form_data.get("From", "")
            callee = form_data.get("To", "")
            twiml = twilio_webhook_handler(
                call_sid, caller, callee, self.config.webhook_url
            )
            return Response(content=twiml, media_type="text/xml")

        @app.post("/webhooks/twilio/recording")
        async def twilio_recording_callback(request: Request):
            form = await request.form()
            recording_sid = form.get("RecordingSid", "")
            recording_url = form.get("RecordingUrl", "")
            call_sid = form.get("CallSid", "")
            logger.info("Recording %s for call %s: %s", recording_sid, call_sid, recording_url)
            return Response(content="", status_code=204)

        @app.post("/webhooks/twilio/amd")
        async def twilio_amd_callback(request: Request):
            form = await request.form()
            answered_by = form.get("AnsweredBy", "")
            call_sid = form.get("CallSid", "")
            logger.info("AMD result for %s: %s", call_sid, answered_by)

            if (
                answered_by in ("machine_end_beep", "machine_end_silence")
                and self.voicemail_message
                and self.config.twilio_sid
                and self.config.twilio_token
            ):
                from patter.handlers.twilio_handler import _xml_escape, _validate_twilio_sid

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
            await twilio_stream_bridge(
                websocket=websocket,
                agent=self.agent,
                openai_key=self.config.openai_key,
                on_call_start=self.on_call_start,
                on_call_end=self.on_call_end,
                on_transcript=self.on_transcript,
                on_message=self.on_message,
                deepgram_key=self.config.deepgram_key,
                elevenlabs_key=self.config.elevenlabs_key,
                twilio_sid=self.config.twilio_sid,
                twilio_token=self.config.twilio_token,
                recording=self.recording,
            )

        # --- Telnyx ---

        @app.post("/webhooks/telnyx/voice")
        async def telnyx_voice(request: Request):
            body = await request.json()
            if not isinstance(body.get("data"), dict) or not isinstance(body.get("data", {}).get("payload"), dict):
                logger.warning(
                    "Telnyx webhook rejected: missing data.payload structure. "
                    "Enable Ed25519 signature verification for production use."
                )
                return Response(status_code=400, content="Invalid webhook structure")
            payload = body["data"]["payload"]
            if not payload.get("call_control_id") or not payload.get("from") or not payload.get("to"):
                logger.warning(
                    "Telnyx webhook rejected: missing required payload fields. "
                    "Enable Ed25519 signature verification for production use."
                )
                return Response(status_code=400, content="Invalid webhook payload")
            if not self._telnyx_sig_warning_logged:
                self._telnyx_sig_warning_logged = True
                logger.warning(
                    "Telnyx webhook Ed25519 signature verification not implemented "
                    "— validate signatures in production"
                )
            call_id = payload.get("call_control_id", "")
            caller = payload.get("from", "")
            callee = payload.get("to", "")
            response_data = telnyx_webhook_handler(
                call_id,
                caller,
                callee,
                self.config.webhook_url,
                connection_id=self.config.telnyx_connection_id,
            )
            from fastapi.responses import JSONResponse
            return JSONResponse(content=response_data)

        @app.websocket("/ws/telnyx/stream/{call_id}")
        async def telnyx_stream_handler(websocket: WebSocket, call_id: str):
            await telnyx_stream_bridge(
                websocket=websocket,
                agent=self.agent,
                openai_key=self.config.openai_key,
                on_call_start=self.on_call_start,
                on_call_end=self.on_call_end,
                on_transcript=self.on_transcript,
                on_message=self.on_message,
                deepgram_key=self.config.deepgram_key,
                elevenlabs_key=self.config.elevenlabs_key,
            )

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
                from patter.providers.twilio_adapter import TwilioAdapter  # type: ignore[import]

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

        from patter.banner import show_banner
        show_banner()

        logger.info("Server starting on port %s", port)
        logger.info("Webhook URL: https://%s", self.config.webhook_url)
        logger.info("Phone: %s", self.config.phone_number)
        logger.info("Agent: %s / %s", self.agent.model, self.agent.voice)

        config = uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="info"
        )
        self._server = uvicorn.Server(config)
        await self._server.serve()

    async def stop(self) -> None:
        """Gracefully stop the embedded server."""
        if self._server:
            self._server.should_exit = True
