import asyncio
from functools import partial
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import VoiceResponse, Connect
from patter.providers.base import TelephonyProvider

class TwilioAdapter(TelephonyProvider):
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
        available = await self._run_sync(self._twilio_client.available_phone_numbers(country).local.list, limit=1)
        if not available: raise ValueError(f"No numbers for {country}")
        purchased = await self._run_sync(self._twilio_client.incoming_phone_numbers.create, phone_number=available[0].phone_number)
        return purchased.phone_number

    async def configure_number(self, number: str, webhook_url: str) -> None:
        numbers = await self._run_sync(self._twilio_client.incoming_phone_numbers.list, phone_number=number)
        if not numbers: raise ValueError(f"Number {number} not found")
        await self._run_sync(numbers[0].update, voice_url=webhook_url, voice_method="POST")

    async def initiate_call(
        self,
        from_number: str,
        to_number: str,
        stream_url: str,
        extra_params: dict | None = None,
    ) -> str:
        twiml = VoiceResponse()
        connect = Connect()
        connect.stream(url=stream_url)
        twiml.append(connect)
        call_kwargs: dict = {"to": to_number, "from_": from_number, "twiml": str(twiml)}
        if extra_params:
            call_kwargs.update(extra_params)
        call = await self._run_sync(self._twilio_client.calls.create, **call_kwargs)
        return call.sid

    async def end_call(self, call_id: str) -> None:
        await self._run_sync(self._twilio_client.calls(call_id).update, status="completed")

    @staticmethod
    def generate_stream_twiml(stream_url: str) -> str:
        response = VoiceResponse()
        connect = Connect()
        connect.stream(url=stream_url)
        response.append(connect)
        return str(response)
