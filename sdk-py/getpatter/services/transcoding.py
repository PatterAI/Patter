try:
    import audioop
except ImportError:
    try:
        import audioop_lts as audioop  # type: ignore
    except ImportError:
        audioop = None  # type: ignore


def mulaw_to_pcm16(mulaw_data: bytes) -> bytes:
    if audioop is None:
        raise ImportError("audioop required: pip install patter[local]")
    return audioop.ulaw2lin(mulaw_data, 2)


def pcm16_to_mulaw(pcm_data: bytes) -> bytes:
    if audioop is None:
        raise ImportError("audioop required: pip install patter[local]")
    return audioop.lin2ulaw(pcm_data, 2)


def resample_8k_to_16k(audio_data: bytes) -> bytes:
    """Resample 8kHz PCM16 to 16kHz using audioop.ratecv."""
    if audioop is None:
        raise ImportError("audioop required: pip install patter[local]")
    if not audio_data:
        return audio_data
    resampled, _ = audioop.ratecv(audio_data, 2, 1, 8000, 16000, None)
    return resampled


def resample_16k_to_8k(audio_data: bytes) -> bytes:
    """Resample 16kHz PCM16 to 8kHz using audioop.ratecv with anti-aliasing."""
    if audioop is None:
        raise ImportError("audioop required: pip install patter[local]")
    if not audio_data:
        return audio_data
    resampled, _ = audioop.ratecv(audio_data, 2, 1, 16000, 8000, None)
    return resampled


class PcmCarry:
    """Odd-byte carry buffer for PCM16 streams.

    HTTP streaming TTS providers (ElevenLabs, Cartesia, LMNT, Rime,
    Telnyx) yield chunks of arbitrary byte length, including odd
    counts. Passing an odd-length buffer to ``audioop.ratecv`` raises
    ``audioop.error: not a whole number of frames``, crashing the TTS
    mid-sentence. Prepend any leftover byte from the previous chunk,
    return the even-length portion, and stash the trailing odd byte for
    the next call. Mirrors TS ``StreamHandler.alignPcm16``.
    """

    __slots__ = ("_carry",)

    def __init__(self) -> None:
        self._carry: bytes = b""

    def align(self, chunk: bytes) -> bytes:
        combined = self._carry + chunk if self._carry else chunk
        aligned_len = len(combined) & ~1
        self._carry = combined[aligned_len:] if aligned_len < len(combined) else b""
        return combined[:aligned_len]

    def reset(self) -> None:
        """Drop any buffered odd byte. Call at each TTS synthesis boundary."""
        self._carry = b""
