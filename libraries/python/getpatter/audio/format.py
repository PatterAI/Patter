"""Audio-format contract — single source of truth for "what rate/encoding does
this stage emit, and what does the carrier wire require".

Why this exists: the outbound pipeline sender used to ASSUME every TTS provider
emits PCM16 @ 16 kHz and ran a hardcoded 16 kHz -> 8 kHz decimator before
mu-law encoding. A provider configured for any other rate (e.g. Cartesia at
8 kHz) was decimated AGAIN and played back at ~2x pitch (chipmunk audio). The
fix is to make every TTS provider DECLARE its output format and the sender
derive the resample ratio from the declared rate.

Parity with TypeScript ``src/audio/format.ts``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

__all__ = [
    "AudioFormat",
    "CARRIER_WIRE_FORMAT",
    "LEGACY_PIPELINE_TTS_FORMAT",
    "parse_wire_format_string",
    "formats_match",
    "resolve_tts_source_format",
]


@dataclass(frozen=True)
class AudioFormat:
    """Declarative description of an audio stream: encoding + sample rate.

    A TTS provider returns this from ``source_audio_format()`` so the pipeline
    knows exactly what it emits; the carrier layer exposes the same shape via
    :data:`CARRIER_WIRE_FORMAT` for the wire it requires. The pipeline
    resamples / transcodes from the former to the latter automatically.

    ``encoding`` is one of ``"pcm_s16le"`` (linear PCM16 LE), ``"mulaw"`` or
    ``"alaw"`` (G.711). ``sample_rate`` is the stream rate in Hz.
    """

    encoding: str
    sample_rate: int


# Every supported carrier's bidirectional media stream carries G.711 mu-law @
# 8 kHz: Twilio always; Telnyx because ``streaming_start`` pins
# ``stream_bidirectional_codec=PCMU`` @ 8 kHz; Plivo because the answer XML pins
# the mu-law content type. This is the ONE place that knowledge lives.
CARRIER_WIRE_FORMAT: dict[str, AudioFormat] = {
    "twilio": AudioFormat(encoding="mulaw", sample_rate=8000),
    "telnyx": AudioFormat(encoding="mulaw", sample_rate=8000),
    "plivo": AudioFormat(encoding="mulaw", sample_rate=8000),
}

# Fallback assumed for providers that declare nothing — the pre-fix default.
LEGACY_PIPELINE_TTS_FORMAT = AudioFormat(encoding="pcm_s16le", sample_rate=16000)


def parse_wire_format_string(s: str) -> Optional[AudioFormat]:
    """Parse a legacy wire-format string (ElevenLabs ``output_format`` style).

    Recognises ``ulaw_8000`` / ``mulaw_8000``, ``alaw_8000``, ``pcm_<rate>``
    and ``pcm16_<rate>``. Returns ``None`` for anything else (e.g. mp3) so the
    caller can fall back.
    """
    lower = s.lower()
    if lower in ("ulaw_8000", "mulaw_8000"):
        return AudioFormat(encoding="mulaw", sample_rate=8000)
    if lower == "alaw_8000":
        return AudioFormat(encoding="alaw", sample_rate=8000)
    if lower.startswith("pcm_") or lower.startswith("pcm16_"):
        digits = lower.split("_", 1)[1]
        if digits.isdigit():
            rate = int(digits)
            if rate > 0:
                return AudioFormat(encoding="pcm_s16le", sample_rate=rate)
    return None


def formats_match(a: AudioFormat, b: AudioFormat) -> bool:
    """True when two formats are byte-compatible (same encoding + rate)."""
    return a.encoding == b.encoding and a.sample_rate == b.sample_rate


def resolve_tts_source_format(tts: object) -> AudioFormat:
    """Resolve the audio format a TTS adapter emits, in priority order.

    1. ``source_audio_format()`` — the uniform contract (preferred).
    2. ``output_format`` string — ElevenLabs-style declaration.
    3. ``sample_rate`` int — PCM providers (Cartesia/OpenAI/LMNT/Rime/Inworld).
    4. :data:`LEGACY_PIPELINE_TTS_FORMAT` — preserves pre-fix behaviour for any
       custom adapter that declares nothing.

    The fallback is what makes this change fully backward compatible: an adapter
    that exposes none of the above behaves exactly as before (PCM16 @ 16 kHz).
    """
    if tts is None:
        return LEGACY_PIPELINE_TTS_FORMAT

    fn = getattr(tts, "source_audio_format", None)
    if callable(fn):
        try:
            fmt = fn()
        except Exception:  # pragma: no cover - defensive; adapter bug
            fmt = None
        if (
            isinstance(fmt, AudioFormat)
            and isinstance(fmt.sample_rate, int)
            and fmt.sample_rate > 0
        ):
            return fmt

    out_fmt = getattr(tts, "output_format", None)
    if isinstance(out_fmt, str):
        parsed = parse_wire_format_string(out_fmt)
        if parsed is not None:
            return parsed

    rate = getattr(tts, "sample_rate", None)
    if isinstance(rate, int) and not isinstance(rate, bool) and rate > 0:
        return AudioFormat(encoding="pcm_s16le", sample_rate=rate)

    return LEGACY_PIPELINE_TTS_FORMAT
