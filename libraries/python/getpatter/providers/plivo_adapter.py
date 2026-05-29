"""Plivo :class:`TelephonyProvider` — number provisioning and call control.

Thin async wrapper over Plivo's v1 REST API used by :mod:`getpatter.client`
to provision numbers, place outbound calls and hang up. Plivo authenticates
with HTTP Basic ``auth_id:auth_token``.

Unlike Twilio (``<Stream>`` inline in the call-create TwiML) and Telnyx
(``streaming_start`` after ``call.answered``), Plivo points the outbound call
at an ``answer_url``; the same ``/webhooks/plivo/voice`` route that serves
inbound calls returns the ``<Stream>`` XML, so outbound media wiring reuses
the inbound answer handler. See :mod:`getpatter.telephony.plivo`.
"""

from __future__ import annotations

import logging

import httpx

from getpatter.providers.base import TelephonyProvider

logger = logging.getLogger("getpatter.providers.plivo_adapter")

PLIVO_API_BASE = "https://api.plivo.com/v1"


def _xml_escape(s: str) -> str:
    """Escape special XML characters.

    Critically, the WSS URL embedded as the ``<Stream>`` text content carries
    a query string whose ``&`` separators MUST be escaped to ``&amp;`` or the
    Plivo XML parser truncates the URL at the first parameter.
    """
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


class PlivoAdapter(TelephonyProvider):
    """:class:`TelephonyProvider` implementation backed by the Plivo REST API."""

    def __init__(self, auth_id: str, auth_token: str):
        self.auth_id = auth_id
        self.auth_token = auth_token
        # Account-scoped base URL: https://api.plivo.com/v1/Account/{auth_id}
        self._client = httpx.AsyncClient(
            base_url=f"{PLIVO_API_BASE}/Account/{auth_id}",
            auth=(auth_id, auth_token),
            timeout=30.0,
        )

    def __repr__(self) -> str:
        masked = f"{self.auth_id[:6]}..." if len(self.auth_id) > 6 else "***"
        return f"PlivoAdapter(auth_id={masked!r})"

    async def provision_number(self, country: str) -> str:
        """Search and rent an available Plivo number for the given ISO country."""
        resp = await self._client.get(
            "/PhoneNumber/",
            params={"country_iso": country, "limit": 1},
        )
        resp.raise_for_status()
        objects = resp.json().get("objects", [])
        if not objects:
            raise ValueError(f"No numbers for {country}")
        number = objects[0]["number"]
        # Rent the discovered number. Plivo returns 201 with {"numbers": [...]}.
        buy = await self._client.post(f"/PhoneNumber/{number}/")
        buy.raise_for_status()
        return number

    async def configure_number(self, number: str, webhook_url: str) -> None:
        """Point the inbound answer flow for *number* at *webhook_url*.

        Plivo routes inbound calls through an **Application**, so we create
        (or reuse) an application bound to ``webhook_url`` and link the number
        to it. Most production deployments pre-configure the application in
        the Plivo console; this helper exists for parity with Twilio's
        ``configure_number`` auto-setup convenience.
        """
        answer_url = (
            webhook_url
            if webhook_url.startswith("http")
            else f"https://{webhook_url}/webhooks/plivo/voice"
        )
        app = await self._client.post(
            "/Application/",
            data={
                "app_name": "patter-inbound",
                "answer_url": answer_url,
                "answer_method": "POST",
            },
        )
        app.raise_for_status()
        app_id = app.json().get("app_id", "")
        if not app_id:
            logger.warning("Plivo Application create returned no app_id")
            return
        link = await self._client.post(
            f"/Number/{number}/",
            data={"app_id": app_id},
        )
        if link.status_code >= 400:
            logger.warning(
                "Plivo configure_number returned %s: %s",
                link.status_code,
                link.text[:300],
            )
        link.raise_for_status()

    async def initiate_call(
        self,
        from_number: str,
        to_number: str,
        stream_url: str,
        *,
        answer_url: str = "",
        hangup_url: str = "",
        ring_timeout: int | None = None,
        machine_detection: bool = False,
        machine_detection_url: str = "",
    ) -> str:
        """Place an outbound Plivo call routed through *answer_url*.

        NOTE: ``stream_url`` is NOT sent to Plivo. Plivo fetches ``answer_url``
        when the callee answers and that handler returns the ``<Stream>`` XML
        that opens the media WebSocket — so the WSS URL travels inside the XML,
        not as a dial parameter. ``stream_url`` is retained for
        :class:`TelephonyProvider` interface parity but intentionally unused.

        Returns Plivo's ``request_uuid`` (the queued-call handle). The live
        ``CallUUID`` used for hangup / transfer arrives later on the answer
        webhook and the WS ``start`` frame; see :mod:`getpatter.telephony.plivo`.

        Args:
            from_number: Caller ID in E.164 (must be a Plivo number you own).
            to_number: Callee number in E.164.
            stream_url: Unused — see docstring.
            answer_url: Public URL returning the ``<Stream>`` answer XML.
            ring_timeout: Max seconds to ring before no-answer.
            machine_detection: Enable answering-machine detection.
            machine_detection_url: Async AMD result callback (no answer-latency
                penalty on human pickups, mirroring Twilio's Async AMD).
            hangup_url: End-of-call status callback. Plivo POSTs CallStatus
                (completed / busy / no-answer / failed / timeout / cancel) here
                — the analogue of Twilio's ``StatusCallback``. Required for the
                dashboard to surface outbound calls that never reach media.
        """
        del stream_url  # see docstring — retained only for TelephonyProvider parity
        payload: dict = {
            "from": from_number,
            "to": to_number,
            "answer_url": answer_url,
            "answer_method": "POST",
        }
        if hangup_url:
            payload["hangup_url"] = hangup_url
            payload["hangup_method"] = "POST"
        if ring_timeout is not None:
            payload["ring_timeout"] = int(ring_timeout)
        if machine_detection:
            # ``machine_detection="true"`` runs detection; pairing it with an
            # async ``machine_detection_url`` means Plivo does NOT delay the
            # answer_url on human pickups — the classification is POSTed to
            # the callback instead. Mirrors Twilio Async AMD semantics.
            payload["machine_detection"] = "true"
            payload["machine_detection_time"] = 5000
            if machine_detection_url:
                payload["machine_detection_url"] = machine_detection_url
                payload["machine_detection_method"] = "POST"
        resp = await self._client.post("/Call/", json=payload)
        resp.raise_for_status()
        return resp.json().get("request_uuid", "")

    async def end_call(self, call_id: str) -> None:
        """Hang up an active Plivo call by CallUUID.

        Plivo returns 204 on success and 404 when the call already ended;
        both are treated as success (the call is gone either way).
        """
        from urllib.parse import quote as _quote

        resp = await self._client.delete(f"/Call/{_quote(call_id, safe='')}/")
        if resp.status_code not in (204, 404):
            resp.raise_for_status()

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
                    "patter.telephony": "plivo",
                    "patter.direction": direction,
                }
            )
        except Exception:  # pragma: no cover — defense in depth
            logger.debug("record_call_end_cost failed", exc_info=True)

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    @staticmethod
    def generate_stream_xml(
        stream_url: str,
        content_type: str = "audio/x-mulaw;rate=8000",
        extra_headers: dict[str, str] | None = None,
    ) -> str:
        """Return Plivo XML that connects the call to the media stream URL.

        Plivo's ``<Stream>`` element takes the WSS URL as its **text content**
        (not a ``url=`` attribute, as Twilio does). ``bidirectional`` enables
        two-way audio and ``keepCallAlive`` keeps the leg up for the lifetime
        of the WebSocket.

        ``extra_headers`` is forwarded via the ``extraHeaders`` attribute as a
        comma-separated ``key=value`` list; Plivo delivers it back on the WS
        ``start`` frame's ``extra_headers`` field. Used as a fallback channel
        for caller / callee when the query string is unavailable.
        """
        attrs = (
            'bidirectional="true" keepCallAlive="true" '
            f'contentType="{_xml_escape(content_type)}"'
        )
        if extra_headers:
            joined = ",".join(f"{k}={v}" for k, v in extra_headers.items())
            attrs += f' extraHeaders="{_xml_escape(joined)}"'
        return (
            "<Response>"
            f"<Stream {attrs}>{_xml_escape(stream_url)}</Stream>"
            "</Response>"
        )
