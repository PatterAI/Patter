from typing import AsyncIterator, Literal, Optional
import re
import httpx
from getpatter.providers.base import TTSProvider

# Supported `output_format` values for the `/text-to-speech/{id}/stream`
# endpoint. `ulaw_8000` is the telephony-ready option for Twilio/Telnyx.
ElevenLabsOutputFormat = Literal[
    "mp3_22050_32",
    "mp3_44100_32",
    "mp3_44100_64",
    "mp3_44100_96",
    "mp3_44100_128",
    "mp3_44100_192",
    "pcm_8000",
    "pcm_16000",
    "pcm_22050",
    "pcm_24000",
    "pcm_44100",
    "ulaw_8000",
]

# Curated map of common ElevenLabs voice display names to their voice IDs. The
# public API only accepts voice IDs (opaque 20-char strings), so callers that
# pass a human-readable name like "rachel" would otherwise hit 404. Add names
# here as they become popular, or call resolve_voice_id to extend at runtime.
_ELEVENLABS_VOICE_ID_BY_NAME = {
    "rachel": "21m00Tcm4TlvDq8ikWAM",
    "drew": "29vD33N1CtxCmqQRPOHJ",
    "clyde": "2EiwWnXFnvU5JabPnv8n",
    "paul": "5Q0t7uMcjvnagumLfvZi",
    "domi": "AZnzlk1XvdvUeBnXmlld",
    "dave": "CYw3kZ02Hs0563khs1Fj",
    "fin": "D38z5RcWu1voky8WS1ja",
    "bella": "EXAVITQu4vr4xnSDxMaL",
    "antoni": "ErXwobaYiN019PkySvjV",
    "thomas": "GBv7mTt0atIp3Br8iCZE",
    "charlie": "IKne3meq5aSn9XLyUdCD",
    "george": "JBFqnCBsd6RMkjVDRZzb",
    "emily": "LcfcDJNUP1GQjkzn1xUU",
    "elli": "MF3mGyEYCl7XYWbV9V6O",
    "callum": "N2lVS1w4EtoT3dr4eOWO",
    "patrick": "ODq5zmih8GrVes37Dizd",
    "harry": "SOYHLrjzK2X1ezoPC6cr",
    "liam": "TX3LPaxmHKxFdv7VOQHJ",
    "dorothy": "ThT5KcBeYPX3keUQqHPh",
    "josh": "TxGEqnHWrfWFTfGW9XjX",
    "arnold": "VR6AewLTigWG4xSOukaG",
    "charlotte": "XB0fDUnXU5powFXDhCwa",
    "matilda": "XrExE9yKIg1WjnnlVkGX",
    "matthew": "Yko7PKHZNXotIFUBG7I9",
    "james": "ZQe5CZNOzWyzPSCn5a3c",
    "joseph": "Zlb1dXrM653N07WRdFW3",
    "jeremy": "bVMeCyTHy58xNoL34h3p",
    "michael": "flq6f7yk4E4fJM5XTYuZ",
    "ethan": "g5CIjZEefAph4nQFvHAz",
    "gigi": "jBpfuIE2acCO8z3wKNLl",
    "freya": "jsCqWAovK2LkecY7zXl4",
    "brian": "nPczCjzI2devNBz1zQrb",
    "grace": "oWAxZDx7w5VEj9dCyTzz",
    "daniel": "onwK4e9ZLuTAKqWW03F9",
    "lily": "pFZP5JQG7iQjIQuC4Bku",
    "serena": "pMsXgVXv3BLzUgSXRplE",
    "adam": "pNInz6obpgDQGcFmaJgB",
    "nicole": "piTKgcLEGmPE4e6mEKli",
    "bill": "pqHfZKP75CvOlQylNhV4",
    "jessie": "t0jbNlBVZ17f02VDIeMI",
    "ryan": "wViXBPUzp2ZZixB1xQuM",
    "sam": "yoZ06aMxZJJ28mfd3POQ",
    "glinda": "z9fAnlkpzviPz146aGWa",
    "giovanni": "zcAOhNBS3c14rBihAFp1",
    "mimi": "zrHiDhphv9ZnVXBqCLjz",
    "sarah": "EXAVITQu4vr4xnSDxMaL",
    # OpenAI voice-name aliases for convenience (map to reasonable EL voices).
    "alloy": "EXAVITQu4vr4xnSDxMaL",
}

_VOICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{20}$")


def resolve_voice_id(voice: str) -> str:
    """Return an ElevenLabs voice ID from either a UUID-like ID or a display name.

    Opaque ElevenLabs voice IDs are 20-char alnum tokens — anything matching
    that shape is returned verbatim. Known display names (case-insensitive) are
    resolved via the internal table. Unknown strings are returned as-is so the
    SDK behaves identically for custom voices the user has created.
    """
    if not voice:
        return voice
    if _VOICE_ID_PATTERN.match(voice):
        return voice
    return _ELEVENLABS_VOICE_ID_BY_NAME.get(voice.lower(), voice)


class ElevenLabsTTS(TTSProvider):
    def __init__(
        self,
        api_key: str,
        voice_id: str = "21m00Tcm4TlvDq8ikWAM",
        model_id: str = "eleven_flash_v2_5",
        output_format: ElevenLabsOutputFormat = "pcm_16000",
        voice_settings: Optional[dict] = None,
        language_code: Optional[str] = None,
        chunk_size: int = 4096,
    ):
        self.api_key = api_key
        self.voice_id = resolve_voice_id(voice_id)
        self.model_id = model_id
        self.output_format = output_format
        self.voice_settings = voice_settings
        self.language_code = language_code
        self.chunk_size = chunk_size
        self._client = httpx.AsyncClient(
            base_url="https://api.elevenlabs.io/v1",
            headers={"xi-api-key": api_key},
            timeout=30.0,
        )

    def __repr__(self) -> str:
        return f"ElevenLabsTTS(model_id={self.model_id!r}, voice_id={self.voice_id!r})"

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        body: dict = {"text": text, "model_id": self.model_id}
        if self.voice_settings:
            body["voice_settings"] = self.voice_settings
        if self.language_code:
            body["language_code"] = self.language_code
        req = self._client.build_request(
            "POST",
            f"/text-to-speech/{self.voice_id}/stream",
            json=body,
            params={"output_format": self.output_format},
        )
        resp = await self._client.send(req, stream=True)
        resp.raise_for_status()
        try:
            async for chunk in resp.aiter_bytes(chunk_size=self.chunk_size):
                yield chunk
        finally:
            await resp.aclose()

    async def close(self) -> None:
        await self._client.aclose()
