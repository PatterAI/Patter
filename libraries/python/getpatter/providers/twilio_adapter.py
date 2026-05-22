"""Twilio :class:`TelephonyProvider` — number provisioning and call control.

Async wrapper over the synchronous Twilio REST client. Sync calls are
dispatched to a thread executor so the event loop is never blocked.
"""

import asyncio
import logging
import re
from functools import partial
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import VoiceResponse, Connect
from getpatter.providers.base import TelephonyProvider

logger = logging.getLogger("getpatter.providers.twilio_adapter")


_PASCAL_TO_SNAKE_RE = re.compile(r"(?<!^)(?=[A-Z])")


def _to_snake_case(name: str) -> str:
    """Translate a PascalCase / camelCase Twilio param to snake_case.

    The ``twilio-python`` SDK's ``client.calls.create(**kwargs)`` accepts
    snake_case keyword arguments only — it translates them to the
    PascalCase form Twilio's REST wire protocol requires. Passing a
    PascalCase key directly raises ``TypeError: unexpected keyword
    argument``. This helper normalises both shapes so the adapter is
    robust regardless of how the caller spelled the param.
    """
    return _PASCAL_TO_SNAKE_RE.sub("_", name).lower()


class TwilioAdapter(TelephonyProvider):
    """:class:`TelephonyProvider` implementation backed by the Twilio REST API."""

    def __init__(self, account_sid: str, auth_token: str):
        self.account_sid = account_sid
        self.auth_token = auth_token
        self._twilio_client = TwilioClient(account_sid, auth_token)

    def __repr__(self) -> str:
        masked = f"{self.account_sid[:6]}..." if len(self.account_sid) > 6 else "***"
        return f"TwilioAdapter(account_sid={masked!r})"

    async def _run_sync(self, func, *args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, partial(func, *args, **kwargs))

    async def provision_number(self, country: str) -> str:
        """Find and purchase a local Twilio number in the given ISO country."""
        available = await self._run_sync(
            self._twilio_client.available_phone_numbers(country).local.list, limit=1
        )
        if not available:
            raise ValueError(f"No numbers for {country}")
        purchased = await self._run_sync(
            self._twilio_client.incoming_phone_numbers.create,
            phone_number=available[0].phone_number,
        )
        return purchased.phone_number

    async def configure_number(self, number: str, webhook_url: str) -> None:
        """Point Twilio's voice webhook for *number* at *webhook_url* (POST)."""
        numbers = await self._run_sync(
            self._twilio_client.incoming_phone_numbers.list, phone_number=number
        )
        if not numbers:
            raise ValueError(f"Number {number} not found")
        await self._run_sync(
            numbers[0].update, voice_url=webhook_url, voice_method="POST"
        )

    async def initiate_call(
        self,
        from_number: str,
        to_number: str,
        stream_url: str,
        extra_params: dict | None = None,
    ) -> str:
        """Place an outbound Twilio call that streams media to *stream_url*."""
        twiml = VoiceResponse()
        connect = Connect()
        connect.stream(url=stream_url)
        twiml.append(connect)
        call_kwargs: dict = {"to": to_number, "from_": from_number, "twiml": str(twiml)}
        if extra_params:
            # Defensive normalisation: the ``twilio-python`` SDK rejects
            # PascalCase kwargs (``StatusCallback``, ``MachineDetection``,
            # …) with ``TypeError: unexpected keyword argument``.
            # ``getpatter.client`` already builds the dict in snake_case
            # form; this guard catches any third-party caller (or future
            # regression) that still passes the wire-protocol spelling.
            for key, value in extra_params.items():
                call_kwargs[_to_snake_case(key)] = value
        call = await self._run_sync(self._twilio_client.calls.create, **call_kwargs)
        return call.sid

    async def end_call(self, call_id: str) -> None:
        """Hang up the named Twilio call by setting status=completed."""
        await self._run_sync(
            self._twilio_client.calls(call_id).update, status="completed"
        )

    def record_call_end_cost(self, *, duration_seconds: float, direction: str) -> None:
        """Emit ``patter.cost.telephony_minutes`` on the active span.

        Called by the embedded server's bridge cleanup once the call's
        wall-clock duration is known.
        """
        try:
            from getpatter.observability.attributes import record_patter_attrs

            record_patter_attrs(
                {
                    "patter.cost.telephony_minutes": duration_seconds / 60.0,
                    "patter.telephony": "twilio",
                    "patter.direction": direction,
                }
            )
        except Exception:  # pragma: no cover — defense in depth
            logger.debug("record_call_end_cost failed", exc_info=True)

    @staticmethod
    def generate_stream_twiml(
        stream_url: str,
        parameters: dict[str, str] | None = None,
    ) -> str:
        """Return TwiML that connects the inbound call to the media stream URL.

        ``parameters`` is forwarded as ``<Parameter name="..." value="..."/>``
        children of ``<Stream>``. Twilio Media Streams ignores query-string
        params on the ``<Stream url=...>`` (they are stripped before the WS
        handshake), so ``<Parameter>`` tags are the supported way to
        pre-populate ``start.customParameters`` on the WS payload. Used by
        the inbound path to carry caller / callee through to the bridge.
        """
        response = VoiceResponse()
        connect = Connect()
        stream = connect.stream(url=stream_url)
        if parameters:
            for name, value in parameters.items():
                if value is None:
                    continue
                stream.parameter(name=name, value=str(value))
        response.append(connect)
        return str(response)
