from typing import AsyncIterator
import httpx
from patter.providers.base import TTSProvider

class ElevenLabsTTS(TTSProvider):
    def __init__(self, api_key: str, voice_id: str = "21m00Tcm4TlvDq8ikWAM", model_id: str = "eleven_turbo_v2_5", output_format: str = "pcm_16000"):
        self.api_key = api_key; self.voice_id = voice_id; self.model_id = model_id; self.output_format = output_format
        self._client = httpx.AsyncClient(base_url="https://api.elevenlabs.io/v1", headers={"xi-api-key": api_key}, timeout=30.0)

    def __repr__(self) -> str:
        return f"ElevenLabsTTS(model_id={self.model_id!r}, voice_id={self.voice_id!r})"

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        req = self._client.build_request("POST", f"/text-to-speech/{self.voice_id}/stream", json={"text": text, "model_id": self.model_id}, params={"output_format": self.output_format})
        resp = await self._client.send(req, stream=True); resp.raise_for_status()
        try:
            async for chunk in resp.aiter_bytes(chunk_size=4096): yield chunk
        finally:
            await resp.aclose()

    async def close(self) -> None: await self._client.aclose()
