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
        resp = await self._client.get("/available_phone_numbers", params={"filter[country_code]": country, "filter[limit]": 1})
        resp.raise_for_status()
        numbers = resp.json()["data"]
        if not numbers: raise ValueError(f"No numbers for {country}")
        chosen = numbers[0]["phone_number"]
        buy = await self._client.post("/number_orders", json={"phone_numbers": [{"phone_number": chosen}]})
        buy.raise_for_status()
        return chosen

    async def configure_number(self, number: str, webhook_url: str) -> None:
        """Associate number with the Call Control Application."""
        # Update the number's connection to point to our Call Control App
        await self._client.patch(
            f"/phone_numbers/{number}",
            json={"connection_id": self.connection_id},
        )

    async def initiate_call(
        self,
        from_number: str,
        to_number: str,
        stream_url: str,
        *,
        ring_timeout: int | None = None,
    ) -> str:
        payload: dict = {
            "connection_id": self.connection_id,
            "from": from_number,
            "to": to_number,
            "stream_url": stream_url,
            "stream_track": "both_tracks",
        }
        if ring_timeout is not None:
            payload["timeout_secs"] = int(ring_timeout)
        resp = await self._client.post("/calls", json=payload)
        resp.raise_for_status()
        return resp.json()["data"]["call_control_id"]

    async def end_call(self, call_id: str) -> None:
        await self._client.post(f"/calls/{call_id}/actions/hangup")

    async def close(self) -> None:
        await self._client.aclose()
