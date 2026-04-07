import struct
from typing import AsyncIterator

import httpx

from patter.providers.base import TTSProvider

OPENAI_TTS_URL = "https://api.openai.com/v1/audio/speech"


class OpenAITTS(TTSProvider):
    def __init__(self, api_key: str, voice: str = "alloy", model: str = "tts-1"):
        self.api_key = api_key
        self.voice = voice
        self.model = model
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"}, timeout=30.0
        )

    def __repr__(self) -> str:
        return f"OpenAITTS(model={self.model!r}, voice={self.voice!r})"

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        request = self._client.build_request(
            "POST",
            OPENAI_TTS_URL,
            json={
                "model": self.model,
                "input": text,
                "voice": self.voice,
                "response_format": "pcm",
            },
        )
        response = await self._client.send(request, stream=True)
        response.raise_for_status()

        async for chunk in response.aiter_bytes(chunk_size=4096):
            # OpenAI returns 24kHz PCM16, resample to 16kHz
            resampled = self._resample_24k_to_16k(chunk)
            yield resampled

        await response.aclose()

    @staticmethod
    def _resample_24k_to_16k(audio_data: bytes) -> bytes:
        """Resample 24kHz PCM16 to 16kHz by taking every 2 out of 3 samples."""
        if len(audio_data) < 2:
            return audio_data
        num_samples = len(audio_data) // 2
        samples = struct.unpack(f"<{num_samples}h", audio_data[: num_samples * 2])
        # Take 2 out of every 3 samples (24000/16000 = 3/2)
        resampled = []
        for i in range(0, len(samples), 3):
            resampled.append(samples[i])
            if i + 1 < len(samples):
                # Interpolate between sample i+1 and i+2
                if i + 2 < len(samples):
                    resampled.append((samples[i + 1] + samples[i + 2]) // 2)
                else:
                    resampled.append(samples[i + 1])
        return struct.pack(f"<{len(resampled)}h", *resampled)

    async def close(self) -> None:
        await self._client.aclose()
