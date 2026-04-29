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
# ``gpt-4o-mini-tts`` is the first OpenAI TTS model that accepts an
# ``instructions`` field (voice direction). Older models (``tts-1``,
# ``tts-1-hd``) 400 if we include it, so we gate on this prefix.
_INSTRUCTIONS_PREFIX = "gpt-4o-mini-tts"


class OpenAITTS(TTSProvider):
    def __init__(
        self,
        api_key: str,
        voice: str = "alloy",
        model: str = "gpt-4o-mini-tts",
        *,
        instructions: str | None = None,
        speed: float | None = None,
    ):
        self.api_key = api_key
        self.voice = voice
        self.model = model
        self.instructions = instructions
        if speed is not None and not (0.25 <= speed <= 4.0):
            raise ValueError("OpenAITTS: speed must be in [0.25, 4.0]")
        self.speed = speed
        # Use read-idle timeouts rather than a 30 s end-to-end wall clock so
        # long TTS bodies streamed as a slow trickle don't get killed mid-way.
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
        )

    def __repr__(self) -> str:
        return f"OpenAITTS(model={self.model!r}, voice={self.voice!r})"

    def _record_synthesis_cost(self, text: str) -> None:
        from getpatter.observability.attributes import record_patter_attrs

        record_patter_attrs(
            {
                "patter.cost.tts_chars": len(text),
                "patter.tts.provider": "openai_tts",
            }
        )

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        self._record_synthesis_cost(text)
        if audioop is None:
            # Without ``audioop`` / ``audioop-lts`` we would emit 24 kHz
            # audio that the telephony pipeline transcodes as 16 kHz —
            # users hear chipmunk voices. Fail loudly instead.
            raise RuntimeError(
                "OpenAITTS requires the 'audioop' (Python ≤3.12) or 'audioop-lts' "
                "(Python 3.13+) module to resample 24 kHz PCM to 16 kHz. "
                "Install 'audioop-lts' via pip to enable TTS."
            )
        body: dict = {
            "model": self.model,
            "input": text,
            "voice": self.voice,
            "response_format": "pcm",
        }
        if self.instructions is not None and self.model.startswith(
            _INSTRUCTIONS_PREFIX
        ):
            body["instructions"] = self.instructions
        if self.speed is not None:
            body["speed"] = self.speed
        request = self._client.build_request("POST", OPENAI_TTS_URL, json=body)
        response = await self._client.send(request, stream=True)
        response.raise_for_status()

        # StatefulResampler preserves audioop.ratecv filter state across
        # chunk boundaries, preventing the pops/garbled audio that occurred
        # with the previous stateless per-chunk approach (acceptance test 09).
        from getpatter.services.transcoding import create_resampler_24k_to_16k

        resampler = create_resampler_24k_to_16k()
        try:
            # 1024-byte chunks ≈ 21 ms at 24 kHz / 16-bit (vs ~85 ms at the
            # previous 4096), which lowers TTFB on the synthesized audio.
            # The StatefulResampler is chunk-size-agnostic — it carries
            # filter state and any odd trailing byte across chunks — so the
            # smaller granularity does not introduce pops or alignment drift.
            async for chunk in response.aiter_bytes(chunk_size=1024):
                if not chunk:
                    continue
                resampled = resampler.process(chunk)
                if resampled:
                    yield resampled
            # Flush any buffered odd byte from the final chunk.
            tail = resampler.flush()
            if tail:
                yield tail
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
