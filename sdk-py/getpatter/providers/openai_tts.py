from typing import AsyncIterator

import httpx

try:
    # Python ≤ 3.12 ships ``audioop``; on 3.13+ the ``audioop-lts`` PyPI
    # package exposes the same C API (pinned in our pyproject).
    import audioop  # type: ignore[import]
except ImportError:  # pragma: no cover
    audioop = None  # type: ignore[assignment]

from getpatter.providers.base import TTSProvider

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

        # ``audioop.ratecv`` resamples chunk-by-chunk while preserving
        # cross-chunk filter state — critical because OpenAI streams the
        # PCM body in arbitrary-sized slices and a stateless per-chunk
        # downsample produced audible pops / dropped audio (the caller
        # heard garbled or silent TTS in acceptance test 09).
        state = None
        carry = b""  # odd trailing byte between chunks (PCM16 = 2 B/sample)
        try:
            async for chunk in response.aiter_bytes(chunk_size=4096):
                if not chunk:
                    continue
                buf = carry + chunk
                # Keep only a whole number of 16-bit samples for the
                # current ratecv call; stash the extra byte for next time.
                usable_len = (len(buf) // 2) * 2
                carry = buf[usable_len:]
                buf = buf[:usable_len]
                if not buf:
                    continue
                if audioop is None:
                    # Fallback: no resample, downstream will try to
                    # transcode 24 kHz as 16 kHz and sound wrong. Log once.
                    yield buf
                    continue
                resampled, state = audioop.ratecv(
                    buf,
                    2,  # sample width (bytes)
                    1,  # channels
                    24000,  # in rate
                    16000,  # out rate
                    state,
                )
                if resampled:
                    yield resampled
        finally:
            await response.aclose()

    async def close(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _resample_24k_to_16k(audio: bytes) -> bytes:
        """Stateless 24 kHz → 16 kHz resample used by unit tests.

        The streaming ``synthesize`` path uses ``audioop.ratecv`` with
        per-stream state carried across chunks (see the class docstring).
        This helper performs a single-shot resample on a complete buffer
        and is kept for backwards compatibility with the unit tests.
        """
        if len(audio) < 2 or audioop is None:
            return audio
        out, _ = audioop.ratecv(
            audio[: (len(audio) // 2) * 2], 2, 1, 24000, 16000, None
        )
        return out
