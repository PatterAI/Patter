import httpx
from getpatter.providers.base import TelephonyProvider

TELNYX_API_BASE = "https://api.telnyx.com/v2"

class TelnyxAdapter(TelephonyProvider):
    def __init__(self, api_key: str, connection_id: str = ""):
        self.api_key = api_key
        self.connection_id = connection_id
        self._client = httpx.AsyncClient(
            base_url=TELNYX_API_BASE,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
        )

    def __repr__(self) -> str:
        return f"TelnyxAdapter(connection_id={self.connection_id!r})"

    async def provision_number(self, country: str) -> str:
        # Telnyx search filter uses nested ``filter[phone_number][country_code]``
        # (not ``filter[country_code]``). See
        # ``.claude/skills/telnyx-numbers-compliance-python/SKILL.md``.
        resp = await self._client.get(
            "/available_phone_numbers",
            params={
                "filter[phone_number][country_code]": country,
                "filter[limit]": 1,
            },
        )
        resp.raise_for_status()
        numbers = resp.json()["data"]
        if not numbers:
            raise ValueError(f"No numbers for {country}")
        chosen = numbers[0]["phone_number"]
        order_body: dict = {"phone_numbers": [{"phone_number": chosen}]}
        # When a Call Control Application is bound to this adapter, attach
        # it to the order so the newly-purchased number is ready to place
        # and receive calls without a follow-up PATCH.
        if self.connection_id:
            order_body["connection_id"] = self.connection_id
        buy = await self._client.post("/number_orders", json=order_body)
        buy.raise_for_status()
        return chosen

    async def configure_number(self, number: str, webhook_url: str) -> None:
        """Associate number with the Call Control Application.

        Uses ``PATCH /phone_numbers/{id}/voice`` which is the correct voice
        settings endpoint per the Telnyx numbers skill. The older
        ``PATCH /phone_numbers/{id}`` endpoint does not accept
        ``connection_id`` consistently across the v2 API.

        ``number`` may be the phone_number ID or the E.164 string; the API
        accepts both identifiers but the phone_number ID is preferred.
        # TODO: verify exact shape against live Telnyx portal — see
        # ``.claude/skills/telnyx-numbers-compliance-python/SKILL.md``.
        """
        from urllib.parse import quote as _quote
        payload = {
            "connection_id": self.connection_id,
            "tech_prefix_enabled": False,
        }
        resp = await self._client.patch(
            f"/phone_numbers/{_quote(number, safe='')}/voice",
            json=payload,
        )
        if resp.status_code >= 400:
            # Surface the server-side error body so misconfigured
            # connection_ids / unknown numbers don't fail silently.
            import logging as _logging
            _logging.getLogger("patter").warning(
                "Telnyx configure_number returned %s: %s",
                resp.status_code,
                resp.text[:300],
            )
        resp.raise_for_status()

    async def initiate_call(
        self,
        from_number: str,
        to_number: str,
        stream_url: str,
        *,
        ring_timeout: int | None = None,
        client_state: str | None = None,
    ) -> str:
        """Place an outbound Call Control dial.

        NOTE: ``stream_url`` / ``stream_track`` are NOT accepted by
        ``POST /calls``. Telnyx attaches media streaming after the call is
        answered via ``POST /calls/{id}/actions/streaming_start`` — typically
        triggered from the ``call.answered`` webhook. The ``stream_url``
        argument is retained for interface parity (``TelephonyProvider``)
        but is intentionally unused here.

        See ``.claude/skills/telnyx-voice-python/SKILL.md`` and the
        ``call.answered`` handler in ``server.py`` for the full flow.

        Args:
            from_number: Caller E.164 number (must be owned on the connection).
            to_number: Callee E.164 number.
            stream_url: Unused — media stream is attached after ``call.answered``.
            ring_timeout: Max seconds to wait before the leg is marked no-answer.
            client_state: Optional opaque string echoed back on every webhook
                for this call. Encoded as base64 per Telnyx contract.
        """
        del stream_url  # see docstring — retained only for TelephonyProvider parity
        payload: dict = {
            "connection_id": self.connection_id,
            "from": from_number,
            "to": to_number,
        }
        if ring_timeout is not None:
            payload["timeout_secs"] = int(ring_timeout)
        if client_state:
            # Telnyx expects client_state as base64-encoded opaque string.
            import base64 as _b64
            payload["client_state"] = _b64.b64encode(client_state.encode("utf-8")).decode("ascii")
        resp = await self._client.post("/calls", json=payload)
        resp.raise_for_status()
        return resp.json()["data"]["call_control_id"]

    async def end_call(self, call_id: str, *, command_id: str | None = None) -> None:
        """Hang up an active Telnyx call.

        ``command_id`` provides idempotency on retries (Telnyx will ignore a
        duplicate action with the same ``command_id``). When omitted, a
        UUID4 is generated automatically.
        """
        import uuid as _uuid
        from urllib.parse import quote as _quote
        body = {"command_id": command_id or str(_uuid.uuid4())}
        await self._client.post(f"/calls/{_quote(call_id, safe='')}/actions/hangup", json=body)

    async def close(self) -> None:
        await self._client.aclose()
