from enum import StrEnum
from typing import AsyncIterator, Optional, Union
import re
import httpx
from getpatter.providers.base import TTSProvider


# Known stable ElevenLabs voice models (from
# https://elevenlabs.io/docs/api-reference/text-to-speech). ``StrEnum`` keeps
# enum members usable as plain strings, so existing callers passing literal
# string model IDs continue to work unchanged.
class ElevenLabsModel(StrEnum):
    V3 = "eleven_v3"
    FLASH_V2_5 = "eleven_flash_v2_5"
    TURBO_V2_5 = "eleven_turbo_v2_5"
    MULTILINGUAL_V2 = "eleven_multilingual_v2"
    MONOLINGUAL_V1 = "eleven_monolingual_v1"


# Supported ``output_format`` values for the ``/text-to-speech/{id}/stream``
# endpoint. ``ULAW_8000`` is the telephony-ready option for Twilio/Telnyx.
class ElevenLabsOutputFormat(StrEnum):
    MP3_22050_32 = "mp3_22050_32"
    MP3_44100_32 = "mp3_44100_32"
    MP3_44100_64 = "mp3_44100_64"
    MP3_44100_96 = "mp3_44100_96"
    MP3_44100_128 = "mp3_44100_128"
    MP3_44100_192 = "mp3_44100_192"
    PCM_8000 = "pcm_8000"
    PCM_16000 = "pcm_16000"
    PCM_22050 = "pcm_22050"
    PCM_24000 = "pcm_24000"
    PCM_44100 = "pcm_44100"
    ULAW_8000 = "ulaw_8000"


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
    """ElevenLabs streaming TTS adapter.

    Supported ``model_id`` values (autocompleted via :data:`ElevenLabsModel`):

    * ``eleven_v3`` — newest, highest quality (slower TTFT than Flash).
    * ``eleven_flash_v2_5`` — current default, fastest (~75 ms TTFT).
    * ``eleven_turbo_v2_5`` — balanced quality/speed.
    * ``eleven_multilingual_v2`` — best multilingual support.
    * ``eleven_monolingual_v1`` — legacy English-only.

    The default remains ``eleven_flash_v2_5`` (lowest TTFT). Pass any
    other string for forward-compat with future ElevenLabs models.

    Telephony optimization
    ----------------------
    The constructor default ``output_format='pcm_16000'`` is the right
    choice for web playback, dashboard previews, and 16 kHz pipelines.
    For real phone calls use the carrier-specific factories instead:

    * :meth:`for_twilio` — emits ``ulaw_8000`` natively. Twilio's media
      stream WebSocket expects μ-law @ 8 kHz, so the SDK normally
      resamples 16 kHz → 8 kHz and PCM → μ-law before sending. Asking
      ElevenLabs to produce μ-law directly skips that step (saves
      ~30-80 ms on the first byte plus per-frame CPU and avoids any
      resampling aliasing).
    * :meth:`for_telnyx` — emits ``pcm_16000``. Telnyx negotiates
      L16/16000 on its bidirectional media WebSocket, so 16 kHz PCM is
      already the format used end-to-end and no transcoding happens.
      ElevenLabs *also* supports ``ulaw_8000`` if you have a Telnyx
      profile pinned to PCMU/8000 — pass ``output_format='ulaw_8000'``
      explicitly in that case.
    """

    def __init__(
        self,
        api_key: str,
        voice_id: str = "21m00Tcm4TlvDq8ikWAM",
        model_id: Union[ElevenLabsModel, str] = ElevenLabsModel.FLASH_V2_5,
        output_format: Union[
            ElevenLabsOutputFormat, str
        ] = ElevenLabsOutputFormat.PCM_16000,
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

    # ------------------------------------------------------------------
    # Telephony factories
    # ------------------------------------------------------------------

    @classmethod
    def for_twilio(
        cls,
        api_key: str,
        *,
        voice_id: str = "21m00Tcm4TlvDq8ikWAM",
        model_id: Union[ElevenLabsModel, str] = ElevenLabsModel.FLASH_V2_5,
        voice_settings: Optional[dict] = None,
        language_code: Optional[str] = None,
        chunk_size: int = 4096,
    ) -> "ElevenLabsTTS":
        """Build an instance pre-configured for Twilio Media Streams.

        Sets ``output_format='ulaw_8000'`` so ElevenLabs emits μ-law @ 8 kHz
        directly — the exact wire format Twilio's media stream uses — letting
        the SDK skip the 16 kHz→8 kHz resample and PCM→μ-law conversion in
        ``TwilioAudioSender``. Saves ~30–80 ms on first byte and per-frame
        CPU, and removes a potential aliasing source.

        ``voice_settings`` defaults to a low-bandwidth-friendly profile
        (speaker boost off, modest stability) which sounds cleaner at 8 kHz
        μ-law than the studio default.
        """
        if voice_settings is None:
            # Speaker boost adds high-frequency emphasis that aliases ugly
            # over an 8 kHz μ-law line. Slightly higher stability tames the
            # excursions that compander quantization noise can amplify.
            voice_settings = {
                "stability": 0.6,
                "similarity_boost": 0.75,
                "use_speaker_boost": False,
            }
        return cls(
            api_key=api_key,
            voice_id=voice_id,
            model_id=model_id,
            output_format=ElevenLabsOutputFormat.ULAW_8000,
            voice_settings=voice_settings,
            language_code=language_code,
            chunk_size=chunk_size,
        )

    @classmethod
    def for_telnyx(
        cls,
        api_key: str,
        *,
        voice_id: str = "21m00Tcm4TlvDq8ikWAM",
        model_id: Union[ElevenLabsModel, str] = ElevenLabsModel.FLASH_V2_5,
        voice_settings: Optional[dict] = None,
        language_code: Optional[str] = None,
        chunk_size: int = 4096,
    ) -> "ElevenLabsTTS":
        """Build an instance pre-configured for Telnyx bidirectional media.

        Telnyx's default media-streaming codec is L16 PCM @ 16 kHz, which
        matches our default Telnyx handler. We pick ``pcm_16000`` so the
        audio flows end-to-end with zero resampling or transcoding.

        Trade-off: if your Telnyx profile is pinned to PCMU/8000 (μ-law),
        construct ``ElevenLabsTTS`` directly with
        ``output_format='ulaw_8000'`` — Telnyx supports that natively too.
        """
        return cls(
            api_key=api_key,
            voice_id=voice_id,
            model_id=model_id,
            output_format=ElevenLabsOutputFormat.PCM_16000,
            voice_settings=voice_settings,
            language_code=language_code,
            chunk_size=chunk_size,
        )

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
